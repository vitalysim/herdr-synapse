"""Collaboration on the canvas (canvas v2 phase 5): the review gate, proposals, freezes, settings, per-author undo,
checkpoints, and what readers see of them.

Contract ``.local/prd/canvas-v2-phase5.md``; ``docs/collaboration.md`` is the owner's guide. The operator in person is
the lead (``is_lead``): the collaboration layer never refuses her, and only she accepts, rejects, freezes, thaws,
restores, changes the settings or forces an undo. Everyone else goes through **one gate per op, after the op runs**
(section 2): the op runs as it does today, then every registered rule looks at the op's element changes (``Review``)
and says live, propose or refuse (``Verdict``). A proposed op keeps none of its changes on the canvas; its after-values
become one proposal record (``P-n``), a ghost on the page that only the lead accepts or rejects.

An op an agent may not make (``element_not_yours``) runs once more with raised authority (D2). Its result can only
become a proposal, or live under ``human_edits: live`` for the operator's marks; the ``locked`` and ``frozen`` rules
check it as the real author, and a rule never sees the raised author.

Adding a rule is one function and one ``register_rule`` call (and one row in ``tests/fixtures/collab/matrix.json``):

    def pinned(review):
        hit = [c.id for c in review.changes if c.primary and c.before and (c.before.get("pin") or {}).get("by") == "human"]
        return Verdict("propose", "pinned", "{} was placed by the operator".format(hit[0]), tuple(hit)) if hit else None

    register_rule("pinned", pinned, 55)

Rules are pure: they read the ``Review`` and nothing else. This module imports ``canvas``; ``canvas`` imports it late.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team import canvas as C
from herdr_team import canvas_check as _check
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as _kinds
from herdr_team import store
from herdr_team.errors import HerdrTeamError
from herdr_team.paths import check_not_symlink

#: The core ops whose element changes go through the gate; every kind op does too unless its ``OpSpec.proposable`` is
#: False (a comment is a request, always live).
PROPOSABLE_CORE = ("move", "restyle", "edit", "delete", "refit", "patch", "place", "pin", "unpin")
#: The decisions that are the operator's in person (1.2 guarantee 3): delegates, managers and members are refused.
LEAD_ONLY_OPS = ("accept", "reject", "freeze", "thaw", "settings", "restore", "migrate")
#: The collaboration settings (5.3) and their defaults.
SETTINGS_KEY = "collab"
SETTINGS_DEFAULTS = {"human_edits": "propose", "frozen": "propose"}
HUMAN_EDITS = ("propose", "live")
FROZEN_MODES = ("propose", "refuse")
PROPOSAL_STATUSES = ("open", "accepted", "rejected", "withdrawn", "superseded")
MAX_OPEN_PROPOSALS_PER_AUTHOR = 20
MAX_OPEN_PROPOSALS = 200
#: Decided proposals kept in the state, and how many of the newest the scene carries.
MAX_DECIDED_KEPT = 100
SCENE_DECIDED = 20
MAX_SUMMARY_LINES = 12
SUMMARY_LABEL_CHARS = 24
MAX_LINE_CHARS = 120
#: How far back one batch reads the log to say what changed since its ``base``.
MAX_BASE_SCAN = 5000
#: An agent op on the element the operator is editing now is refused ``element_busy``; retry after this long.
BUSY_RETRY_S = 5
#: Checkpoints (7): snapshot files, their size cap, and how many each author keeps.
CHECKPOINTS_DIR = "checkpoints"
MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
LEAD_NAMED_CHECKPOINTS = 30
AUTHOR_NAMED_CHECKPOINTS = 3
AUTO_CHECKPOINTS = 10
#: A batch of at least this many ops from anyone but the lead saves an automatic checkpoint first.
AUTO_CHECKPOINT_OPS = 30
#: Proposal ghosts (4.6) and freeze outlines (5.2) in the display list.
PROPOSAL_Z = 1_000_000
GHOST_OPACITY = 0.5
#: An in-place ghost is drawn beside its host, this far clear of it, with a dashed leader between them (QA phase 6, F3).
GHOST_ASIDE_GAP = 40.0
FREEZE_Z = -3
FREEZE_BAND = 12
#: An id freeze whose outline would be this many times the area of its marks' own outlines draws one round each instead.
FREEZE_SPLIT = 4.0
#: An automatic claim grows over new marks this close to it, up to this size a side (4.1; QA phase 5 L8).
AUTO_CLAIM_JOIN = 200
AUTO_CLAIM_MAX = 2400
#: Off switch for measuring the gate's cost (``tools/canvas_qa.py --perf collab``); always on otherwise.
GATE_ON = True

Box = Tuple[float, float, float, float]


# --------------------------------------------------------------------------
# who decides


def is_lead(author: Any) -> bool:
    """The operator in person: the writable page, the console, or a trusted shell (never a delegate)."""
    return bool(getattr(author, "is_human", False) and getattr(author, "operator", False))


def proposable(op_name: Any) -> bool:
    """Whether an op's changes go through the gate: the core edit ops, and every kind op not marked otherwise."""
    if not isinstance(op_name, str):
        return False
    if op_name in PROPOSABLE_CORE:
        return True
    spec = _kinds.op(op_name)
    return bool(spec is not None and spec.proposable)


def settings_of(state: Any) -> Dict[str, str]:
    """The team's collaboration settings, missing keys read as the defaults."""
    stored = (getattr(state, "settings", {}) or {}).get(SETTINGS_KEY)
    out = dict(SETTINGS_DEFAULTS)
    if isinstance(stored, dict):
        if stored.get("human_edits") in HUMAN_EDITS:
            out["human_edits"] = stored["human_edits"]
        if stored.get("frozen") in FROZEN_MODES:
            out["frozen"] = stored["frozen"]
    return out


def _lead_only(what: str) -> HerdrTeamError:
    return C._error("operator_only", "{} is for the operator in person; a delegate, the manager and members may ask (a comment "
                                     "or a proposal)".format(what))


# --------------------------------------------------------------------------
# what a rule sees (2.2)


@dataclass(frozen=True)
class Change:
    id: str
    action: str
    before: Optional[Dict[str, Any]]
    after: Optional[Dict[str, Any]]
    primary: bool


@dataclass(frozen=True)
class StaleChange:
    id: str
    by: Optional[str]
    by_kind: Optional[str]
    seq: int
    lines: Tuple[str, ...]

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "by": self.by, "by_kind": self.by_kind, "seq": self.seq, "lines": list(self.lines)}


class Lanes:
    """Every active claim and every home, by author: an author's lane is its claims plus its home (4.1)."""

    def __init__(self, claims: Iterable[Mapping[str, Any]], homes: Mapping[str, Any]) -> None:
        self.regions: List[Tuple[str, Box, str]] = []
        for claim in claims:
            region = claim.get("region")
            if isinstance(region, list) and len(region) == 4:
                self.regions.append((str(claim.get("author")), tuple(float(v) for v in region), str(claim.get("id"))))  # type: ignore[arg-type]
        for name, home in sorted((homes or {}).items()):
            if isinstance(home, list) and len(home) == 4:
                self.regions.append((str(name), tuple(float(v) for v in home), "home"))  # type: ignore[arg-type]

    def owners_at(self, box: Sequence[float]) -> List[Tuple[str, str]]:
        """``(author, claim id or "home")`` of every lane that holds ``box``'s centre."""
        cx, cy = (float(box[0]) + float(box[2])) / 2.0, (float(box[1]) + float(box[3])) / 2.0
        return [(author, what) for author, region, what in self.regions if region[0] <= cx <= region[2] and region[1] <= cy <= region[3]]

    def owner_at(self, box: Sequence[float], exclude: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """The first other author's lane holding ``box``, unless ``exclude``'s own lane holds it too."""
        found = self.owners_at(box)
        if exclude is not None and any(author == exclude for author, _what in found):
            return None
        return next(((author, what) for author, what in found if author != exclude), None)


class Territory:
    """The operator's top-level marks (not comments, not arrows), bucketed by 1000-unit cells."""

    CELL = 1000.0

    def __init__(self, elements: Iterable[Mapping[str, Any]]) -> None:
        self.buckets: Dict[Tuple[int, int], List[Tuple[str, Box]]] = {}
        for el in elements:
            if el.get("author_kind") != C.KIND_HUMAN or el.get("frame") or el.get("type") in ("comment", "arrow"):
                continue
            box = C.bounds(dict(el))
            for key in self._keys(box):
                self.buckets.setdefault(key, []).append((str(el.get("id")), box))

    def _keys(self, box: Sequence[float]) -> Iterator[Tuple[int, int]]:
        for i in range(int(math.floor(box[0] / self.CELL)), int(math.floor(box[2] / self.CELL)) + 1):
            for j in range(int(math.floor(box[1] / self.CELL)), int(math.floor(box[3] / self.CELL)) + 1):
                yield i, j

    def hits(self, box: Sequence[float]) -> List[str]:
        found: List[str] = []
        for key in self._keys(box):
            for eid, other in self.buckets.get(key, ()):
                if eid not in found and C._intersects(box, other):
                    found.append(eid)
        return sorted(found, key=lambda i: (C._id_number(i), i))


@dataclass(frozen=True)
class Review:
    author: Any
    lead: bool
    op: str
    raised: bool
    changes: Tuple[Change, ...]
    settings: Dict[str, str]
    freezes: Tuple[Dict[str, Any], ...]
    locks: Tuple[Dict[str, Any], ...]
    lanes: Lanes
    territory: Territory
    human: Optional[Dict[str, Any]]
    stale: Tuple[StaleChange, ...]
    #: Element id -> the freeze (``X-n``) that covers it, as the canvas stood before this op.
    frozen: Mapping[str, str] = field(default_factory=dict)
    #: Element id -> the element as the *stored* scene holds it, for a rule that must resolve a container chain
    #: (``hosts``, A1) from what is there rather than from what the op says about it.
    scene: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: An element as this op would leave it (a container a new element joins), or None.
    lookup: Callable[[str], Optional[Dict[str, Any]]] = lambda _eid: None
    #: The version the batch was made against (``base``), or None.
    base: Optional[int] = None


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str
    message: str = ""
    ids: Tuple[str, ...] = ()
    code: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    reasons: Tuple[str, ...] = ()


Rule = Callable[[Review], Optional[Verdict]]
_RULES: Dict[str, Tuple[int, Rule]] = {}


def register_rule(name: str, fn: Rule, order: int) -> None:
    """Add a rule; ``order`` sorts them (the built-in ones use 10 to 70). A second rule with the same name is an error."""
    if name in _RULES:
        raise ValueError("a collaboration rule called {} is registered already".format(name))
    _RULES[name] = (int(order), fn)


def unregister_rule(name: str) -> None:
    _RULES.pop(name, None)


def rules() -> List[Tuple[int, str, Rule]]:
    return sorted((order, name, fn) for name, (order, fn) in _RULES.items())


# --------------------------------------------------------------------------
# the built-in rules (2.3)


def _whose(el: Optional[Mapping[str, Any]], reader: Optional[str] = None) -> str:
    if el is None:
        return "?"
    if el.get("author_kind") == C.KIND_HUMAN or el.get("author") == C.HUMAN:
        return "yours" if reader == C.HUMAN else "the operator's"  # her own look says "(yours)" (QA phase 5 verdict)
    name = str(el.get("author") or "?")
    return "yours" if reader is not None and name == reader else "{}'s".format(name)


def _is_note(el: Optional[Mapping[str, Any]]) -> bool:
    """Comments and arrows are never judged by territory or lane."""
    if el is None:
        return False
    kind = _kinds.kind_of(el)
    return el.get("type") in ("comment", "arrow") or (kind is not None and kind.role in ("connector", "overlay"))


def _rule_busy(review: Review) -> Optional[Verdict]:
    editing = editing_ids(review.human)
    if not editing:
        return None
    hit = next((c.id for c in review.changes if c.primary and c.id in editing), None)
    if hit is not None:
        return Verdict("refuse", "busy", "{} is being edited by the operator right now; try again in a few seconds".format(hit),
                       (hit,), "element_busy", {"retry_after_s": BUSY_RETRY_S, "id": hit})
    return None


def _rule_locked(review: Review) -> Optional[Verdict]:
    if not review.raised or getattr(review.author, "operator", False) or not review.locks:
        return None
    for change in review.changes:
        for value in (change.before, change.after):
            if value is None:
                continue
            box = C.bounds(value)
            for lock in review.locks:
                region = lock.get("region")
                if isinstance(region, list) and len(region) == 4 and C._intersects(box, region):
                    return Verdict("refuse", "locked", "{} is inside {} locked by {}".format(C.cell_name(box[0], box[1]), lock.get("id"), C._lock_by(lock)),
                                   (change.id,), "canvas_locked", {"lock": lock.get("id")})
    return None


def _frozen_hits(review: Review) -> List[Tuple[str, str]]:
    """``(element id, freeze id)`` of every primary change the freezes cover."""
    return frozen_hits(review.changes, review.freezes, review.frozen)


def frozen_hits(changes: Iterable[Change], freezes: Sequence[Mapping[str, Any]], frozen: Mapping[str, str]) -> List[Tuple[str, str]]:
    """``(element id, freeze id)`` of every primary change ``freezes`` cover (``frozen``: ``frozen_map`` before the changes).
    Shared by the ``frozen`` rule and ``undo`` (which is not proposable, but a freeze binds it all the same)."""
    regions = [(f["id"], f["region"]) for f in freezes if isinstance(f.get("region"), list) and len(f["region"]) == 4]
    hits: List[Tuple[str, str]] = []
    for change in changes:
        if not change.primary or change.after is not None and change.after.get("type") == "comment":
            continue
        if change.before is not None and change.id in frozen:
            hits.append((change.id, frozen[change.id]))
            continue
        if change.after is None:
            continue
        container = change.after.get("frame") or change.after.get("group")
        if change.before is None and isinstance(container, str) and container in frozen:
            hits.append((change.id, frozen[container]))
            continue
        box = C.bounds(change.after)
        found = next((xid for xid, region in regions if C._intersects(box, region)), None)
        if found is not None:
            hits.append((change.id, found))
    return hits


def frozen_message(hits: Sequence[Tuple[str, str]], new: Set[str]) -> str:
    """``E-2 is frozen (X-1): the operator holds it as it is``; a new mark ``would go in X-1, which the operator froze``."""
    parts = []
    held = [(eid, xid) for eid, xid in hits if eid not in new]
    added = [(eid, xid) for eid, xid in hits if eid in new]
    if held:
        ids = list(dict.fromkeys(eid for eid, _x in held))
        parts.append("{} {} frozen ({}): the operator holds {} as {} {}".format(
            C._ids_text(ids), "is" if len(ids) == 1 else "are", ", ".join(dict.fromkeys(x for _e, x in held)),
            "it" if len(ids) == 1 else "them", "it" if len(ids) == 1 else "they", "is" if len(ids) == 1 else "are"))
    if added:
        ids = list(dict.fromkeys(eid for eid, _x in added))
        parts.append("{} would go in {}, which the operator froze".format(C._ids_text(ids), ", ".join(dict.fromkeys(x for _e, x in added))))
    return "; ".join(parts)


def _rule_frozen(review: Review) -> Optional[Verdict]:
    if not review.freezes:
        return None
    hits = _frozen_hits(review)
    if not hits:
        return None
    ids = tuple(dict.fromkeys(eid for eid, _x in hits))
    freezes = tuple(dict.fromkeys(xid for _e, xid in hits))
    message = frozen_message(hits, {c.id for c in review.changes if c.before is None})
    if review.settings.get("frozen") == "refuse":
        return Verdict("refuse", "frozen", message + "; ask them in a comment", ids, "frozen", {"freezes": list(freezes)})
    return Verdict("propose", "frozen", message, ids, details={"freezes": list(freezes)})


def stale_message(stale: Sequence[StaleChange], base: Optional[int]) -> str:
    parts = []
    for entry in stale:
        who = "the operator" if entry.by in (None, C.HUMAN) else entry.by
        how = "; ".join(entry.lines) if entry.lines else "changed"
        parts.append("{} changed since v{}: {} {} (v{})".format(entry.id, base if base is not None else "?", who, how, entry.seq))
    return "; ".join(parts)


def _rule_stale(review: Review) -> Optional[Verdict]:
    bad = [s for s in review.stale if s.by_kind != C.KIND_MEMBER]
    if not bad:
        return None
    return Verdict("refuse", "stale_base", stale_message(bad, review.base) + "; look again and redo", tuple(s.id for s in bad), "stale_base",
                   {"base": review.base, "changes": [s.to_json() for s in bad], "current": {s.id: s.seq for s in bad}})


def _rule_human_made(review: Review) -> Optional[Verdict]:
    touched: List[str] = []
    on: List[str] = []
    for change in review.changes:
        if not change.primary:
            continue
        if change.before is not None:
            if change.before.get("author_kind") == C.KIND_HUMAN:
                touched.append(change.id)
            elif change.after is not None and not _is_note(change.after):
                # A mark of one's own moved into her container (a frame, a block) joins her work: like a new mark there.
                joined = next((change.after.get(key) for key in ("frame", "group")
                               if isinstance(change.after.get(key), str) and change.after.get(key) != change.before.get(key)), None)
                holder = review.lookup(joined) if joined else None
                if holder is not None and holder.get("author_kind") == C.KIND_HUMAN:
                    touched.append(change.id)
                    on.append(str(joined))
            continue
        after = change.after
        if after is None or _is_note(after):
            continue
        container = next((after.get(key) for key in ("frame", "group") if isinstance(after.get(key), str)), None)
        holder = review.lookup(container) if container else None
        if holder is not None and holder.get("author_kind") == C.KIND_HUMAN:
            touched.append(change.id)
            on.append(str(container))
            continue
        hit = review.territory.hits(C.bounds(after))
        if hit:
            touched.append(change.id)
            on.extend(hit)
    if not touched:
        return None
    ids = tuple(dict.fromkeys(touched))
    if review.settings.get("human_edits") == "live":
        return Verdict("live", "human_made", "", ids)
    updated = [c.id for c in review.changes if c.id in ids and c.before is not None and c.before.get("author_kind") == C.KIND_HUMAN]
    if updated:
        message = "{} {} the operator's".format(C._ids_text(updated), "is" if len(updated) == 1 else "are")
    else:
        message = "it goes on the operator's {}".format(C._ids_text(list(dict.fromkeys(on))))
    return Verdict("propose", "human_made", message, ids)


#: A1 (5.6): the fields a container's author may change on a peer's mark inside it without asking - where the mark
#: is, how big it is and how it stacks - and, on a bound arrow, the route and label spot its ends imply.
GEOMETRY_KEYS = frozenset(("x", "y", "w", "h", "z"))
ARROW_GEOMETRY_KEYS = frozenset(("points", "label_at"))
#: What every applied change rewrites whatever it did: when it happened, and the label size a box's own size implies.
GEOMETRY_TAIL_KEYS = frozenset(("updated_seq", "updated_at", "fit"))
#: What ``diff_lines`` calls a geometry change, so the words a reader is shown and the rule can never disagree.
GEOMETRY_LINES = ("moved by ", "resized ")
#: How far up a ``frame``/``group`` chain ``hosts`` looks before it gives up (a cycle cannot outlast it either).
MAX_CONTAINER_DEPTH = 16


def is_geometry_only(before: Optional[Mapping[str, Any]], after: Optional[Mapping[str, Any]]) -> bool:
    """Whether the only difference between two values of one element is where it is drawn (A1, 5.6).

    True when nothing but ``GEOMETRY_KEYS`` and, on a bound arrow, the route and label spot its ends imply, differ -
    plus the bookkeeping every change rewrites. An add, a delete, a restyle, a reword, a repin and a move out of its
    container are never geometry-only. ``diff_lines`` answers the same question in words, so this asks it too and
    every line it would print must be a move or a resize: the two read one set of keys and cannot drift apart.
    """
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False  # an add or a delete changes whether the mark exists, which is not its geometry
    geometry = GEOMETRY_KEYS
    if after.get("type") == "arrow" and (after.get("from") or after.get("to")):
        geometry = geometry | ARROW_GEOMETRY_KEYS
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    if changed - (geometry | GEOMETRY_TAIL_KEYS):
        return False
    if "fit" in changed and not changed & geometry:
        return False  # a refit on its own changes how the words are drawn, which is not where the mark is
    return all(line.startswith(GEOMETRY_LINES) for line in diff_lines(before, after))


def hosts(name: Optional[str], el: Optional[Mapping[str, Any]], by_id: Mapping[str, Mapping[str, Any]]) -> Optional[str]:
    """The container of ``el`` that ``name`` made (its ``frame``/``group`` chain), or None.

    Read from the stored scene and never from the op, so an op cannot claim a container it does not own by naming one.
    """
    if not name or not isinstance(el, Mapping):
        return None
    seen: Set[str] = set()
    current: Mapping[str, Any] = el
    for _depth in range(MAX_CONTAINER_DEPTH):
        parent_id = next((current.get(key) for key in ("frame", "group") if isinstance(current.get(key), str)), None)
        if not isinstance(parent_id, str) or parent_id in seen:
            return None
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if not isinstance(parent, Mapping):
            return None
        if parent.get("author_kind") == C.KIND_MEMBER and str(parent.get("author") or "") == name:
            return parent_id
        current = parent
    return None


def hosted_geometry(review: Review) -> Dict[str, str]:
    """``element id -> container id`` for every change that only moves a peer's mark inside a container of the
    author's own (A1): what the author may do live, and what ``_rule_peer`` therefore does not propose."""
    name = getattr(review.author, "name", None)
    if not name or not getattr(review.author, "is_member", False):
        return {}
    by_id = review.scene
    out: Dict[str, str] = {}
    for change in review.changes:
        if not change.primary or change.before is None or change.before.get("author_kind") != C.KIND_MEMBER:
            continue
        if change.before.get("author") == name or not is_geometry_only(change.before, change.after):
            continue
        # The container as the scene holds it, and the mark as it stood: moving a mark out of its host is not hosted.
        container = hosts(name, change.before, by_id)
        if container is not None and hosts(name, change.after, by_id) == container:
            out[change.id] = container
    return out


def _rule_host_geometry(review: Review) -> Optional[Verdict]:
    """A1: inside a frame or a group its author made, that author may move and resize a peer's marks, live.

    Tidying your own drawing stops being possible the moment a peer's contribution lands in it - the demo spent three
    operator decisions on pure re-layout - while rewriting what a peer's mark *says* is exactly what proposals are
    for. So the host right is geometry and nothing else; everything a reader would be told in words stays a proposal.
    """
    hosted = hosted_geometry(review)
    if not hosted:
        return None
    return Verdict("live", "host_geometry", "", tuple(hosted), details={"hosted": dict(hosted)})


def _rule_peer(review: Review) -> Optional[Verdict]:
    if not review.raised:
        return None
    name = getattr(review.author, "name", None)
    hosted = hosted_geometry(review)
    theirs = [(c.id, c.before) for c in review.changes if c.primary and c.before is not None and c.before.get("author_kind") == C.KIND_MEMBER
              and c.before.get("author") != name and c.id not in hosted]
    if not theirs:
        return None
    owners = list(dict.fromkeys(str(el.get("author")) for _i, el in theirs if el is not None))
    return Verdict("propose", "peer", "{} {} {}'s".format(C._ids_text([i for i, _el in theirs]), "is" if len(theirs) == 1 else "are",
                                                           " and ".join(owners)), tuple(i for i, _el in theirs))


def _rule_foreign_lane(review: Review) -> Optional[Verdict]:
    author = review.author
    if getattr(author, "manager", False) or getattr(author, "operator", False) or not getattr(author, "is_member", False):
        return None
    for change in review.changes:
        if not change.primary or change.before is not None or change.after is None or _is_note(change.after):
            continue
        if hosts(getattr(author, "name", None), change.after, review.scene) is not None:
            # Inside a frame or a group the author made, where a mark goes is the author's to decide (A1). A lane is
            # about free ground: a graph of one's own that grows a node while it is laid out again is not a mark
            # placed in somebody else's lane, and treating it as one locked an author out of their own drawing the
            # moment a peer drew beside it (QA F5).
            continue
        found = review.lanes.owner_at(C.bounds(change.after), exclude=author.name)
        if found is not None:
            owner, what = found
            where = "home" if what == "home" else "lane ({})".format(what)
            who = "the operator's" if owner == C.HUMAN else "{}'s".format(owner)
            return Verdict("propose", "foreign_lane", "it is in {} {}".format(who, where), (change.id,), details={"owner": owner, "lane": what})
    return None


for _name, _fn, _order in (("busy", _rule_busy, 10), ("locked", _rule_locked, 20), ("frozen", _rule_frozen, 30),
                           ("stale_base", _rule_stale, 40), ("human_made", _rule_human_made, 50),
                           ("host_geometry", _rule_host_geometry, 55), ("peer", _rule_peer, 60),
                           ("foreign_lane", _rule_foreign_lane, 70)):
    register_rule(_name, _fn, _order)


def gate(review: Review) -> Verdict:
    """Every rule, then the combination (2.4): a refusal wins (``stale_base`` yields to a proposal, which then carries a base
    note), then any proposal makes the whole op a proposal, else live. A raised run that would go live for any other reason
    than the operator's own ``human_edits: live`` is a proposal (the safety net for D2)."""
    found: List[Verdict] = []
    for _order, _name, fn in rules():
        verdict = fn(review)
        if verdict is not None:
            found.append(verdict)
    refusals = [v for v in found if v.outcome == "refuse"]
    proposals = [v for v in found if v.outcome == "propose"]
    hard = [v for v in refusals if v.reason != "stale_base"]
    if hard:
        return hard[0]
    if refusals and not proposals:
        return refusals[0]
    if proposals:
        first = proposals[0]
        reasons = tuple(dict.fromkeys(v.reason for v in proposals))
        ids = tuple(dict.fromkeys(i for v in proposals for i in v.ids))
        return dataclasses.replace(first, ids=ids, reasons=reasons, details=dict(first.details, base_note=bool(refusals)))
    # A raised run goes live only for a reason that names itself: the operator's own ``human_edits: live`` for her
    # marks, or a host tidying a peer's mark inside its own container (A1). Anything else is the safety net for D2.
    touched = next((v for v in found if v.outcome == "live" and v.reason in ("human_made", "host_geometry")), None)
    if review.raised and touched is None:
        human = [c.id for c in review.changes if c.before is not None and c.before.get("author_kind") == C.KIND_HUMAN]
        reason = "human_made" if human else "peer"
        what = C._ids_text(human) + " is the operator's" if human else "it changes marks that are not yours"
        return Verdict("propose", reason, what, tuple(human), reasons=(reason,))
    if touched is not None:
        return Verdict("live", touched.reason, "", touched.ids, reasons=(touched.reason,), details=dict(touched.details))
    return Verdict("live", "live")


# --------------------------------------------------------------------------
# freezes (5): what each one covers


def frozen_map(elements: Mapping[str, Mapping[str, Any]], freezes: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    """Element id -> the first freeze covering it: an id freeze covers its elements and their descendants (frame children,
    block parts, group members), a region freeze every element whose box meets the region. Comments are never frozen."""
    ordered = sorted((f for f in freezes if isinstance(f, Mapping)), key=lambda f: C._id_number(f.get("id")))
    if not ordered:
        return {}
    children: Dict[str, List[str]] = {}
    for eid, el in elements.items():
        for key in ("frame", "group"):
            parent = el.get(key)
            if isinstance(parent, str) and parent != eid:
                children.setdefault(parent, []).append(eid)
    out: Dict[str, str] = {}
    for freeze in ordered:
        xid = str(freeze.get("id"))
        ids = freeze.get("ids")
        if isinstance(ids, list):
            frontier = [i for i in ids if isinstance(i, str) and i in elements]
            seen: Set[str] = set()
            while frontier:
                eid = frontier.pop()
                if eid in seen:
                    continue
                seen.add(eid)
                if elements[eid].get("type") != "comment":
                    out.setdefault(eid, xid)
                frontier.extend(children.get(eid, ()))
        region = freeze.get("region")
        if isinstance(region, list) and len(region) == 4:
            for eid, el in elements.items():
                if el.get("type") != "comment" and C._intersects(C.bounds(dict(el)), region):
                    out.setdefault(eid, xid)
    return out


def freeze_box(freeze: Mapping[str, Any], elements: Mapping[str, Mapping[str, Any]]) -> Optional[Box]:
    """A region freeze's region, or the current union of an id freeze's elements' boxes (it follows them)."""
    region = freeze.get("region")
    if isinstance(region, list) and len(region) == 4:
        return tuple(float(v) for v in region)  # type: ignore[return-value]
    boxes = [C.bounds(dict(elements[i])) for i in freeze.get("ids") or [] if isinstance(i, str) and i in elements]
    if not boxes:
        return None
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


# --------------------------------------------------------------------------
# diff lines (3.4)


def _cut(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1].rstrip() + "…"


def _signed(n: int) -> str:
    return "0" if n == 0 else "{:+d}".format(n)


def _num_text(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _style(el: Mapping[str, Any]) -> Mapping[str, Any]:
    return el.get("style") if isinstance(el.get("style"), dict) else {}


#: Words of shared opening a change is allowed to spend before both sides are quoted from where they differ.
PREFIX_ROOM = 12


def _changed_text(old: str, new: str, limit: int = 40) -> str:
    """``"a" → "b"``, quoted from the word the two stop sharing.

    A rewrite of a card's body or a sticky's detail usually keeps its opening, and quoting both sides from character
    zero then spends the whole line on text that did not change - which is how a full rewrite of the operator's words
    read as nothing but ``resized 300x160 → 320x200`` (QA phase 6, F3).
    """
    one, two = " / ".join(old.split("\n")), " / ".join(new.split("\n"))
    same = 0
    while same < min(len(one), len(two)) and one[same] == two[same]:
        same += 1
    if same <= PREFIX_ROOM:
        return "{} → {}".format(C._q(one, limit), C._q(two, limit))
    cut = one.rfind(" ", 0, same) + 1
    return "…{} → …{}".format(C._q(one[cut:], limit), C._q(two[cut:], limit))


_STYLE_KEYS = (("tone", "tone"), ("variant", "variant"), ("color", "stroke"), ("fill", "fill"), ("width", "width"), ("dash", "dash"),
               ("route", "route"))
#: Element fields that hold words the reader sees beside ``text`` (a card's body, a sticky's detail, an owner, a
#: status): a proposal that rewrites one of them says so instead of naming only the box it grew (QA phase 6, F3).
_WORD_KEYS = ("body", "detail", "label", "owner", "status")
#: Inline collections a block holds in its own element (phase 2, D1): their contents are parts, so a change names the
#: collection and how many items it holds now, never the whole table.
_ITEM_KEYS = ("columns", "rows", "badges", "notes", "messages", "participants", "objects", "links", "groups", "order")


def diff_lines(before: Optional[Mapping[str, Any]], after: Optional[Mapping[str, Any]], parts: int = 0) -> List[str]:
    """What changed between two values of one element, in words, each line at most 120 characters (pure, deterministic;
    ``tests/fixtures/collab/diff-vectors.json``)."""
    if before is None and after is None:
        return []
    if before is None:
        assert after is not None
        text = str(after.get("text") or "")
        container = after.get("frame") or after.get("group")
        where = container if isinstance(container, str) else C.cell_name(after.get("x") or 0, after.get("y") or 0)
        return [_cut("added {}{} in {}".format(after.get("type") or "element", " " + C._q(text, 40) if text.strip() else "", where))]
    if after is None:
        return ["deleted"]
    lines: List[str] = []
    dx = float(after.get("x") or 0) - float(before.get("x") or 0)
    dy = float(after.get("y") or 0) - float(before.get("y") or 0)
    if dx or dy:
        if dx % C.GRID == 0 and dy % C.GRID == 0:
            lines.append("moved by c{}r{}".format(_signed(int(dx // C.GRID)), _signed(int(dy // C.GRID))))
        else:
            lines.append("moved by {},{}".format(_num_text(C._r2(dx)), _num_text(C._r2(dy))))
    size_before, size_after = (before.get("w"), before.get("h")), (after.get("w"), after.get("h"))
    if size_before != size_after:
        lines.append("resized {}x{} → {}x{}".format(_num_text(size_before[0]), _num_text(size_before[1]), _num_text(size_after[0]),
                                                  _num_text(size_after[1])))
    if str(before.get("text") or "") != str(after.get("text") or ""):
        lines.append("text " + _changed_text(str(before.get("text") or ""), str(after.get("text") or "")))
    for key in _WORD_KEYS:
        old_word, new_word = before.get(key), after.get(key)
        if (isinstance(old_word, str) or isinstance(new_word, str)) and str(old_word or "") != str(new_word or ""):
            lines.append("{} {}".format(key, _changed_text(str(old_word or ""), str(new_word or ""))))
    for key in _ITEM_KEYS:
        old_items, new_items = before.get(key), after.get(key)
        if isinstance(old_items, list) and isinstance(new_items, list) and old_items != new_items:
            lines.append("{}: {} → {}".format(key, len(old_items), len(new_items)) if len(old_items) != len(new_items)
                         else "{} rewritten ({} item{})".format(key, len(new_items), "" if len(new_items) == 1 else "s"))
    old_style, new_style = _style(before), _style(after)
    for name, key in _STYLE_KEYS:
        if old_style.get(key) != new_style.get(key):
            lines.append("{} {} → {}".format(name, _num_text(old_style.get(key)), _num_text(new_style.get(key))))
    if before.get("frame") != after.get("frame"):
        lines.append("moved into {}".format(after["frame"]) if isinstance(after.get("frame"), str) else "out of {}".format(before.get("frame")))
    was_pinned, is_pinned = isinstance(before.get("pin"), dict), isinstance(after.get("pin"), dict)
    if was_pinned != is_pinned:
        lines.append("pinned" if is_pinned else "unpinned")
    old_settings = before.get("settings") if isinstance(before.get("settings"), dict) else {}
    new_settings = after.get("settings") if isinstance(after.get("settings"), dict) else {}
    for key in sorted(set(old_settings) | set(new_settings)):
        if old_settings.get(key) != new_settings.get(key):
            lines.append("settings: {} {} → {}".format(key, _num_text(old_settings.get(key)), _num_text(new_settings.get(key))))
    if parts:
        lines.append("{} part{} changed".format(parts, "" if parts == 1 else "s"))
    return [_cut(line) for line in lines]


def _label(el: Optional[Mapping[str, Any]]) -> str:
    text = " ".join(str((el or {}).get("text") or "").split())
    return " " + C._q(text, SUMMARY_LABEL_CHARS) if text else ""


def summary_lines(changes: Sequence[Change]) -> List[str]:
    """One line per primary change: ``E-4 "Pricing" (the operator's): moved by c+2r0``; a block's parts fold into its root."""
    primary = [c for c in changes if c.primary]
    ids = {c.id for c in primary}
    parts: Dict[str, int] = {}
    shown: List[Change] = []
    for change in primary:
        value = change.after or change.before or {}
        root = value.get("group")
        if isinstance(root, str) and isinstance(value.get("part"), str) and change.action != "add":
            parts[root] = parts.get(root, 0) + 1
            continue
        shown.append(change)
    lines: List[str] = []
    for change in shown:
        value = change.after or change.before
        whose = "" if change.before is None else " ({})".format(_whose(change.before))
        lines.append(_cut("{}{}{}: {}".format(change.id, _label(value), whose, "; ".join(
            diff_lines(change.before, change.after, parts.pop(change.id, 0))) or "changed")))
    for root, count in sorted(parts.items(), key=lambda item: C._id_number(item[0])):
        lines.append("{}: {} part{} changed".format(root, count, "" if count == 1 else "s"))
    if len(lines) > MAX_SUMMARY_LINES:
        lines = lines[: MAX_SUMMARY_LINES - 1] + ["… {} more".format(len(lines) - MAX_SUMMARY_LINES + 1)]
    return lines


# --------------------------------------------------------------------------
# the batch's collaboration state (``ctx.collab``)


class _History:
    """What changed after ``base``, read once per batch from the log's end (3.2), at most ``MAX_BASE_SCAN`` events."""

    def __init__(self, team: Any, base: int) -> None:
        self.after: Dict[str, List[Tuple[int, str, str]]] = {}
        #: Each element's value after each change since ``base`` (None for a delete), by seq.
        self.values: Dict[str, Dict[int, Optional[Dict[str, Any]]]] = {}
        self.at_base: Dict[str, Optional[Dict[str, Any]]] = {}
        self.complete = True
        count = 0
        reached_start = True
        for event in C._events_backwards(team):
            count += 1
            if count > MAX_BASE_SCAN:
                reached_start = False
                if int(event["seq"]) > base:
                    self.complete = False
                break
            seq = int(event["seq"])
            author = event.get("author") if isinstance(event.get("author"), dict) else {}
            if seq > base:
                if event.get("op") == "clear":
                    self.complete = False
                    reached_start = False
                    break
                for change in event.get("changes") or []:
                    if isinstance(change, dict) and change.get("target") == "element" and isinstance(change.get("id"), str):
                        self.after.setdefault(change["id"], []).append((seq, str(author.get("name") or ""), str(author.get("kind") or "")))
                        self.values.setdefault(change["id"], {})[seq] = None if change.get("action") == "delete" else change.get("value")
                continue
            wanted = set(self.after) - set(self.at_base)
            if not wanted:
                reached_start = False
                break
            if event.get("op") == "clear":
                for eid in wanted:
                    self.at_base[eid] = None
                reached_start = False
                break
            for change in event.get("changes") or []:
                if isinstance(change, dict) and change.get("target") == "element" and change.get("id") in wanted and change["id"] not in self.at_base:
                    self.at_base[change["id"]] = None if change.get("action") == "delete" else change.get("value")
        if reached_start:
            for eid in set(self.after) - set(self.at_base):
                self.at_base[eid] = None  # it did not exist yet at base
        self.known_at_base = set(self.at_base)


class Batch:
    """What one batch keeps between its ops for the gate: the author, its base, the operator's presence, lanes,
    territory, the proposals it made (``in_proposal``), its auto-claim and the checkpoint files it writes."""

    def __init__(self, ctx: Any, author: Any, base: Optional[int], human: Optional[Dict[str, Any]], op_count: int) -> None:
        self.author = author
        self.lead = is_lead(author)
        self.base = base
        self.human = human
        self._ctx = ctx
        self._territory: Optional[Territory] = None
        self._history: Optional[_History] = None
        self.held: Dict[str, str] = {}
        self.auto_claim: Optional[str] = None
        self.renewed: Set[str] = set()
        self.deletions: List[str] = []
        self.auto_due: Optional[str] = ("a batch of {} ops".format(op_count) if op_count >= AUTO_CHECKPOINT_OPS and not self.lead
                                        and getattr(author, "is_member", False) else None)

    @property
    def lanes(self) -> Lanes:
        """Every lane as the batch stands now (a claim made earlier in the batch counts): a few claims and homes."""
        state = self._ctx.state
        return Lanes(state.active_claims(self._ctx.now), state.homes)

    @property
    def territory(self) -> Territory:
        if self._territory is None:
            self._territory = Territory(self._ctx.state.elements.values())
        return self._territory

    def history(self) -> _History:
        if self._history is None:
            self._history = _History(self._ctx.team, int(self.base or 0))
        return self._history


def begin_batch(ctx: Any, author: Any, base: Optional[int], human: Optional[Dict[str, Any]], op_count: int) -> Batch:
    batch = Batch(ctx, author, base, human, op_count)
    ctx.collab = batch
    return batch


def stale_of(ctx: Any) -> Tuple[StaleChange, ...]:
    """The aimed elements changed after the batch's ``base`` by someone other than its author (3.2)."""
    batch: Batch = ctx.collab
    if batch is None or batch.base is None:
        return ()
    base = int(batch.base)
    ids = sorted((i for i in ctx.aimed if int((ctx.state.elements.get(i) or {}).get("updated_seq") or 0) > base),
                 key=lambda i: (C._id_number(i), i))
    if not ids:
        return ()
    history = batch.history()
    name = batch.author.name
    out: List[StaleChange] = []
    for eid in ids:
        current = ctx.state.elements.get(eid)
        found = history.after.get(eid)
        if not found:
            if history.complete:
                continue  # changed only by this batch
            out.append(StaleChange(eid, None, None, int((current or {}).get("updated_seq") or 0),
                                   ("changed since v{} (too far back to say how); look again".format(base),)))
            continue
        others = [entry for entry in found if entry[1] != name]
        if not others:
            continue
        seq, by, kind = others[0]
        # What the others changed: from the value just before their first change since base (the author's own, when it
        # made or changed the element after base; QA phase 5 L1), else the value at base, to now.
        first_other = min(entry[0] for entry in others)
        own = [entry[0] for entry in found if entry[1] == name and entry[0] < first_other]
        if own:
            lines = tuple(diff_lines(history.values.get(eid, {}).get(max(own)), current)) or ("changed",)
        elif eid in history.known_at_base:
            lines = tuple(diff_lines(history.at_base.get(eid), current)) or ("changed",)
        else:
            lines = ("changed since v{} (too far back to say how); look again".format(base),)
        out.append(StaleChange(eid, by or None, kind or None, seq, lines))
    return tuple(out)


def _changes(ctx: Any) -> Tuple[Change, ...]:
    out: List[Change] = []
    created = set(ctx.created)
    for eid, value in ctx.pending.items():
        before = ctx.state.elements.get(eid)
        if before is None and value is None:
            continue
        action = "add" if before is None else ("delete" if value is None else "update")
        kind = _kinds.kind_of(value or before or {})
        typ = (value or before or {}).get("type")
        derived = action == "update" and eid not in ctx.aimed and eid not in created and (
            typ == "comment" or (kind is not None and kind.role == "connector"))
        out.append(Change(eid, action, before, value, not derived))
    return tuple(out)


def build_review(ctx: Any, raised: bool) -> Review:
    batch: Batch = ctx.collab
    state = ctx.state
    freezes = tuple(dict(f) for f in state.freezes.values())
    return Review(
        author=batch.author, lead=batch.lead, op=ctx.op_name, raised=raised, changes=_changes(ctx), settings=settings_of(state),
        freezes=freezes, locks=tuple(dict(lock) for lock in state.locks.values()), lanes=batch.lanes, territory=batch.territory,
        human=batch.human, stale=stale_of(ctx), frozen=frozen_map(state.elements, freezes) if freezes else {},
        scene=state.elements, lookup=lambda eid: ctx.el(eid) if isinstance(eid, str) else None, base=batch.base)


def may_raise(ctx: Any, op_name: str, err: HerdrTeamError) -> bool:
    """D2: an agent's proposable op refused only for authority runs once more, as a proposal at most."""
    batch = getattr(ctx, "collab", None)
    return (GATE_ON and batch is not None and err.code == "element_not_yours" and proposable(op_name) and not batch.lead
            and getattr(batch.author, "is_member", False) and not getattr(batch.author, "operator", False))


@contextlib.contextmanager
def raised(ctx: Any) -> Iterator[None]:
    """The op's author with the operator's authority, for one re-run of one op; restored in a ``finally``."""
    real = ctx.author
    ctx.author = dataclasses.replace(real, operator=True)
    try:
        yield
    finally:
        ctx.author = real


def review_op(ctx: Any, op_name: str, was_raised: bool) -> None:
    """After an op ran (and before its event is written): the gate, then divert it to a proposal, refuse it, or let it
    go live with its extras (renewed claims, an auto-claim, ``touched_human``, stale and selection warnings)."""
    batch: Optional[Batch] = getattr(ctx, "collab", None)
    if batch is None or not GATE_ON:
        return
    if batch.lead or not proposable(op_name):
        _live(ctx, None, None)
        _auto_checkpoint(ctx)
        return
    review = build_review(ctx, was_raised)
    if not review.changes:
        _live(ctx, None, review)  # nothing to review: an op that changed no element (a refit with nothing to grow)
        _auto_checkpoint(ctx)
        return
    verdict = gate(review)
    if verdict.outcome == "refuse":
        raise C._error(verdict.code or "op_invalid", verdict.message, **dict(verdict.details, reason=verdict.reason, ids=list(verdict.ids)))
    if verdict.outcome == "propose":
        divert(ctx, verdict, review)
        _auto_checkpoint(ctx)
        return
    _live(ctx, verdict, review)
    _auto_checkpoint(ctx)


def _live(ctx: Any, verdict: Optional[Verdict], review: Optional[Review]) -> None:
    batch: Batch = ctx.collab
    stale = review.stale if review is not None else (stale_of(ctx) if batch.base is not None else ())
    for entry in stale:
        if batch.lead or entry.by_kind == C.KIND_MEMBER:
            who = "the operator" if entry.by in (None, C.HUMAN) else entry.by
            ctx.warn("stale_base", _cut("{} changed since v{} by {} (v{}): {}".format(entry.id, batch.base, who, entry.seq, "; ".join(entry.lines))),
                     [entry.id])
    if review is not None and batch.human:
        selected = set(i for i in batch.human.get("selection") or [] if isinstance(i, str))
        hits = [c.id for c in review.changes if c.primary and c.id in selected]
        if hits:
            ctx.warn("operator_selected", "the operator has {} selected right now".format(C._ids_text(hits)), hits)
    if verdict is not None and verdict.reason == "human_made" and verdict.ids:
        ctx.extra["touched_human"] = list(verdict.ids)
    if verdict is not None and verdict.reason == "host_geometry":
        _record_host_move(ctx, verdict.details.get("hosted") or {})
    _renew(ctx)
    author = batch.author
    drawing = proposable(ctx.op_name) or _kinds.op(ctx.op_name) is not None  # an undo that brings marks back claims nothing
    if drawing and getattr(author, "is_member", False) and not getattr(author, "manager", False) and not getattr(author, "operator", False):
        _auto_claim(ctx)


def _record_host_move(ctx: Any, hosted: Mapping[str, str]) -> None:
    """Write ``moved_by`` on every peer mark a host just tidied (A1), so attribution stays honest.

    The mark's ``author`` never changes - the words in it are still theirs - but per-author undo, the author chips
    and the layout engine all have to know that where it sits now is somebody else's doing (``moved_by``, and
    ``pin.by`` for the operator's own placements, are what exempt a route from being re-cut).
    """
    name = str(getattr(ctx.collab.author, "name", "") or "")
    for eid in hosted:
        el = ctx.pending.get(eid)
        if isinstance(el, dict) and name:
            el["moved_by"] = name


def _boxes(ctx: Any) -> List[Box]:
    return [C.bounds(el) for el in ctx.pending.values() if el is not None]


def _renew(ctx: Any) -> None:
    """Sliding expiry (4.1): a live op inside one of its author's claims renews it once less than half its time is left."""
    batch: Batch = ctx.collab
    boxes = _boxes(ctx)
    if not boxes:
        return
    for claim in ctx.state.active_claims(ctx.now):
        if claim.get("author") != batch.author.name or claim.get("id") in batch.renewed:
            continue
        region = claim.get("region")
        expires = C._parse_iso(claim.get("expires_at"))
        if not isinstance(region, list) or len(region) != 4 or expires is None or expires - ctx.now >= C.CLAIM_TTL_S / 2.0:
            continue
        if any(C._intersects(box, region) for box in boxes):
            ctx.other("claim", "update", claim["id"], dict(claim, expires_at=C._iso(ctx.now + C.CLAIM_TTL_S)))
            batch.renewed.add(str(claim["id"]))


def _snap(box: Sequence[float]) -> List[int]:
    grid = C.GRID
    return [int(math.floor((box[0] - grid) / grid) * grid), int(math.floor((box[1] - grid) / grid) * grid),
            int(math.ceil((box[2] + grid) / grid) * grid), int(math.ceil((box[3] + grid) / grid) * grid)]


def _claim_fit(ctx: Any, region: Sequence[float]) -> List[int]:
    """An automatic claim's region, grown outward so its dashed edge is not drawn through a mark it holds most of.

    The same rule the ``claim`` op follows (``canvas_check.claim_snap``, V2), applied here because an automatic claim
    is drawn exactly like an asked-for one: the operator saw one cut a chart's axis labels off. It grows over a mark
    it holds more than half of and never over one it merely reaches into, so whose lane a mark is in barely moves.
    """
    marks = [el for el in ctx.live() if el.get("type") not in ("frame", "comment", "arrow")]
    snapped, _over = _check.claim_snap([float(v) for v in region], marks)
    return snapped


def _near_auto_claim(ctx: Any, name: str, region: Sequence[int], lanes: Lanes) -> Optional[Tuple[Dict[str, Any], List[int]]]:
    """The newest automatic claim of ``name``'s within ``AUTO_CLAIM_JOIN`` of ``region`` that can grow over it (no side
    past ``AUTO_CLAIM_MAX``, no other author's lane inside), and its grown region; or None."""
    pad = AUTO_CLAIM_JOIN
    reach = (region[0] - pad, region[1] - pad, region[2] + pad, region[3] + pad)
    mine = [c for c in ctx.state.active_claims(ctx.now) if c.get("author") == name and c.get("auto")]
    for claim in sorted(mine, key=lambda c: -C._id_number(c.get("id"))):
        old = claim.get("region")
        if not isinstance(old, list) or len(old) != 4 or not C._intersects(reach, old):
            continue
        grown = _claim_fit(ctx, [min(old[0], region[0]), min(old[1], region[1]), max(old[2], region[2]), max(old[3], region[3])])
        if grown[2] - grown[0] > AUTO_CLAIM_MAX or grown[3] - grown[1] > AUTO_CLAIM_MAX:
            continue
        if any(author != name and C._intersects(grown, other) for author, other, _what in lanes.regions):
            continue
        return claim, grown
    return None


def _refit_auto(ctx: Any, batch: "Batch") -> None:
    """Every automatic claim of this author fitted again to the marks as they stand now.

    A claim is fitted when it is made, and the op that makes it is not the op that decides how big its marks are: a
    later op patches the block, its members settle, it hugs itself around them, and the dashed edge of a claim nobody
    asked for is suddenly drawn through a mark the claim holds. That is the defect ``claim_edge`` reports, arriving
    from the system's own side - it appeared on four previously clean QA scenes - and an op that creates nothing was
    the one path that never fitted the claim again.

    All of the author's automatic claims and not only this batch's, because the op that grows a mark is usually in a
    later batch than the op that drew it. An asked-for claim is left alone: its region is the operator's or the
    agent's own statement of where they are working, and ``claim_edge`` offers them the correction to make.
    """
    name = batch.author.name
    for claim in ctx.state.active_claims(ctx.now):
        if claim.get("author") != name or not claim.get("auto"):
            continue
        old = claim.get("region")
        if not isinstance(old, list) or len(old) != 4:
            continue
        grown = _claim_fit(ctx, old)
        if grown != list(old):
            ctx.other("claim", "update", claim["id"], dict(claim, region=grown))
            ctx.entry_extra.setdefault("auto_claim", claim["id"])


def _auto_claim(ctx: Any) -> None:
    """Creation in free space claims it (4.1): the union of the new marks' boxes, padded a grid step, as an ``auto`` claim."""
    batch: Batch = ctx.collab
    name = batch.author.name
    lanes = Lanes(ctx.state.active_claims(ctx.now), ctx.state.homes)
    boxes = []
    for eid in ctx.created:
        el = ctx.pending.get(eid)
        if el is None or _is_note(el):
            continue
        box = C.bounds(el)
        if not lanes.owners_at(box):
            boxes.append(box)
    if not boxes:
        _refit_auto(ctx, batch)
        return
    union = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    region = _claim_fit(ctx, _snap(union))
    current = ctx.state.claims.get(batch.auto_claim) if batch.auto_claim else None
    if current is not None and not C._expired(current, ctx.now):
        old = current.get("region") or region
        grown = _claim_fit(ctx, [min(old[0], region[0]), min(old[1], region[1]), max(old[2], region[2]), max(old[3], region[3])])
        if grown != list(old):
            ctx.other("claim", "update", current["id"], dict(current, region=grown, expires_at=C._iso(ctx.now + C.CLAIM_TTL_S)))
        ctx.entry_extra["auto_claim"] = current["id"]
        return
    near = _near_auto_claim(ctx, name, region, lanes)
    if near is not None:
        # Drawing next to an automatic claim of one's own grows it, so one mark per batch is one lane, not a new claim
        # each time that pushes the older ones out (QA phase 5 L8).
        claim, grown = near
        ctx.other("claim", "update", claim["id"], dict(claim, region=grown, expires_at=C._iso(ctx.now + C.CLAIM_TTL_S)))
        batch.auto_claim = str(claim["id"])
        ctx.entry_extra["auto_claim"] = claim["id"]
        return
    own = [c for c in ctx.state.active_claims(ctx.now) if c.get("author") == name]
    own.sort(key=lambda c: (not c.get("auto"), str(c.get("at")), C._id_number(c.get("id"))))
    while len(own) >= C.MAX_CLAIMS_PER_AUTHOR:
        oldest = own.pop(0)
        ctx.other("claim", "delete", oldest["id"], None)
    cid = ctx.new_id("K")
    label = " ".join(str(ctx.intent or "").split())[: C.MAX_LABEL_CHARS] or "drawing"
    ctx.other("claim", "add", cid, {"id": cid, "author": name, "region": region, "label": label, "intent": ctx.intent, "at": ctx.ts,
                                    "expires_at": C._iso(ctx.now + C.CLAIM_TTL_S), "auto": True})
    batch.auto_claim = cid
    ctx.entry_extra["auto_claim"] = cid


# --------------------------------------------------------------------------
# proposals (4)


def open_proposals(state: Any) -> List[Dict[str, Any]]:
    return sorted((p for p in state.proposals.values() if p.get("status") == "open"), key=lambda p: C._id_number(p.get("id")))


def outdated_of(record: Mapping[str, Any], elements: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Why an open proposal can no longer be accepted as the operator saw it (computed, never stored): a target changed
    or went since it was made, or a new element's container went. Empty when it is current."""
    out: List[Dict[str, Any]] = []
    created = {c.get("id") for c in record.get("changes") or [] if isinstance(c, dict) and c.get("action") == "add"}
    for change in record.get("changes") or []:
        if not isinstance(change, dict):
            continue
        eid = change.get("id")
        if change.get("action") in ("update", "delete"):
            current = elements.get(eid) if isinstance(eid, str) else None
            if current is None:
                out.append({"id": eid, "lines": ["deleted since the proposal"]})
            elif change.get("was") is not None and int(current.get("updated_seq") or 0) != int(change["was"]):
                out.append({"id": eid, "lines": ["changed since the proposal (v{} → v{})".format(change["was"], current.get("updated_seq"))]})
        elif change.get("action") == "add":
            value = change.get("value") if isinstance(change.get("value"), dict) else {}
            for key in ("frame", "group"):
                container = value.get(key)
                if isinstance(container, str) and container not in elements and container not in created:
                    out.append({"id": container, "lines": ["{} it goes into was deleted".format(container)]})
    return out


def values_at(team: Any, wanted: Mapping[str, int]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Each element as it stood at the version given for it (its ``updated_seq`` then), read from the log's end, at most
    ``MAX_BASE_SCAN`` events back."""
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    left = dict(wanted)
    for count, event in enumerate(C._events_backwards(team)):
        if not left or count >= MAX_BASE_SCAN or event.get("op") == "clear":
            break
        seq = int(event["seq"])
        for change in event.get("changes") or []:
            eid = change.get("id") if isinstance(change, dict) and change.get("target") == "element" else None
            if eid in left and left[eid] == seq:
                out[eid] = change.get("value") if change.get("action") != "delete" else None
                del left[eid]
    return out


def outdated_details(team: Any, record: Mapping[str, Any], elements: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """``outdated_of`` with each changed target's ``diff_lines`` from the value the proposal was made against to now."""
    found = outdated_of(record, elements)
    was = {c.get("id"): int(c["was"]) for c in record.get("changes") or [] if isinstance(c, dict) and c.get("was") is not None}
    seen = values_at(team, {e["id"]: was[e["id"]] for e in found if e.get("id") in was and elements.get(e["id"]) is not None})
    for entry in found:
        current = elements.get(entry.get("id")) if isinstance(entry.get("id"), str) else None
        if current is not None and seen.get(entry["id"]) is not None:
            entry["lines"] = diff_lines(seen[entry["id"]], current) or entry["lines"]
    return found


def _decided(record: Mapping[str, Any], status: str, ctx: Any, note: Optional[str]) -> Dict[str, Any]:
    out = {key: value for key, value in record.items() if key != "changes"}
    out.update(status=status, decided_by=ctx.author.name, decided_at=ctx.ts, decided_seq=ctx.seq, note=note)
    return out


def divert(ctx: Any, verdict: Verdict, review: Review) -> str:
    """The ``propose`` verdict (4.3): the op's pending changes become proposal ``P-n``; nothing reaches the canvas."""
    batch: Batch = ctx.collab
    state = ctx.state
    name = batch.author.name
    kept = [c for c in review.changes if c.primary or c.after is None or c.after.get("type") != "comment"]
    targets = [c.id for c in kept if c.primary and c.action in ("update", "delete")]
    created = [c.id for c in kept if c.action == "add"]
    deleted = [c.id for c in kept if c.action == "delete"]
    everyone = open_proposals(state)
    mine = [p for p in everyone if p.get("author") == name]
    superseded = [p for p in mine if set(p.get("targets") or []) & set(targets)]
    if len(mine) - len(superseded) >= MAX_OPEN_PROPOSALS_PER_AUTHOR or len(everyone) - len(superseded) >= MAX_OPEN_PROPOSALS:
        raise C._error("proposal_limit", "{} proposal{} open ({} of them yours; at most {} each and {} in all): withdraw some or wait "
                                         "for the operator".format(len(everyone), " is" if len(everyone) == 1 else "s are", len(mine),
                                                                   MAX_OPEN_PROPOSALS_PER_AUTHOR, MAX_OPEN_PROPOSALS),
                       open=len(everyone), yours=len(mine))
    pid = ctx.new_id("P")
    stale = [s for s in review.stale if s.by_kind != C.KIND_MEMBER]
    base_note = [_cut("{}: {} (v{})".format(s.id, "; ".join(s.lines), s.seq)) for s in stale]
    record = {
        "id": pid, "author": name, "author_kind": C.KIND_MEMBER if getattr(batch.author, "is_member", False) else C.KIND_HUMAN,
        "batch": ctx.batch_id, "seq": ctx.seq, "at": ctx.ts, "op": ctx.op_name, "intent": ctx.intent,
        "reason": verdict.reason, "reasons": list(verdict.reasons or (verdict.reason,)), "status": "open",
        "decided_by": None, "decided_at": None, "decided_seq": None, "note": None,
        "base": batch.base, "base_note": base_note, "targets": targets, "created": created, "deleted": deleted,
        "changes": [{"target": "element", "action": c.action, "id": c.id,
                     "was": int(c.before.get("updated_seq") or 0) if c.before is not None and c.action != "add" else None,
                     "value": c.after} for c in kept],
        "summary": summary_lines(kept),
    }
    for older in superseded:
        ctx.other("proposal", "update", older["id"], _decided(older, "superseded", ctx, "superseded by {}".format(pid)))
    ctx.pending.clear()
    ctx.created.clear()
    ctx.changed.clear()
    ctx.warnings = []
    ctx.block_info = None
    ctx.other("proposal", "add", pid, record)
    ctx.extra["proposal"] = pid
    for change in kept:
        if change.action == "add" and change.after is not None:
            batch.held[change.id] = pid
            for key in ("alias", "client_id"):
                if isinstance(change.after.get(key), str):
                    batch.held[change.after[key]] = pid
    ctx.proposed = {"index": ctx.index, "op": ctx.op_name, "proposal": pid, "reason": verdict.reason, "reasons": record["reasons"],
                    "message": verdict.message, "targets": targets, "created": created, "summary": record["summary"], "base_note": base_note}
    if superseded:
        ctx.proposed["superseded"] = [p["id"] for p in superseded]
    return pid


def held_refusal(ctx: Any, ref: Any) -> Optional[HerdrTeamError]:
    """``in_proposal`` for a later op naming what only a proposal of this batch holds (4.3)."""
    batch = getattr(ctx, "collab", None)
    if batch is None or not isinstance(ref, str):
        return None
    pid = batch.held.get(ref.strip())
    if pid is None:
        return None
    return C._error("in_proposal", "{} is in proposal {}, not on the canvas yet; put what depends on it in the same op, or wait for the "
                                   "operator".format(ref.strip(), pid), ref=ref.strip(), proposal=pid)


def _proposal(ctx: Any, op: Mapping[str, Any]) -> Dict[str, Any]:
    pid = op.get("id")
    record = ctx.state.proposals.get(pid) if isinstance(pid, str) else None
    if record is None:
        raise C._error("element_unknown", "{} is not a proposal".format(pid), ref=pid)
    if record.get("status") != "open":
        raise C._invalid("id", "{} is {}, not open".format(pid, record.get("status")))
    return record


def _note(op: Mapping[str, Any]) -> Optional[str]:
    return C._text(op.get("note"), "note", C.MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True) or None


def op_accept(ctx: Any, op: Dict[str, Any]) -> None:
    """``accept {id, note?}`` (4.4): replay the proposal's stored after-values as the lead, unless it is outdated."""
    if not is_lead(ctx.author):
        raise _lead_only("accepting a proposal")
    record = _proposal(ctx, op)
    note = _note(op)
    stale = outdated_details(ctx.team, record, ctx.state.elements) if outdated_of(record, ctx.state.elements) else []
    if stale:
        raise C._error("proposal_outdated", "{} is outdated: {}; reject it, or ask {} for a new one".format(
            record["id"], "; ".join("{}: {}".format(s["id"], "; ".join(s["lines"])) for s in stale), record.get("author")),
            proposal=record["id"], targets=stale)
    gone: Set[str] = set()
    touched: List[str] = []
    for change in record.get("changes") or []:
        eid = change.get("id")
        if not isinstance(eid, str):
            continue
        if change.get("action") == "delete":
            if ctx.el(eid) is not None:
                ctx.drop(eid)
                gone.add(eid)
            continue
        value = change.get("value")
        if not isinstance(value, dict):
            continue
        new = dict(value, updated_seq=ctx.seq, updated_at=ctx.ts)
        if change.get("action") == "add":
            new["accepted"] = record["id"]
        if new.get("alias"):
            owner = (ctx.state.aliases.get(new["alias"]) or {}).get(str(new.get("author")))
            if owner is not None and owner != eid and ctx.el(owner) is not None:
                new["alias"] = None
        ctx.put(new)
        touched.append(eid)
    if gone:
        C._unbind(ctx, ctx.live(), gone)
    C._reroute_bound(ctx, touched, skip=[i for i in touched if (ctx.el(i) or {}).get("type") == "arrow"])
    ctx.other("proposal", "update", record["id"], _decided(record, "accepted", ctx, note))
    ctx.extra["accepts"] = record["id"]


def op_reject(ctx: Any, op: Dict[str, Any]) -> None:
    """``reject {id, note?}`` (4.5): the lead says no; nothing on the canvas changes and nobody is woken."""
    if not is_lead(ctx.author):
        raise _lead_only("rejecting a proposal")
    record = _proposal(ctx, op)
    ctx.other("proposal", "update", record["id"], _decided(record, "rejected", ctx, _note(op)))
    ctx.extra["rejects"] = record["id"]


def op_withdraw(ctx: Any, op: Dict[str, Any]) -> None:
    """``withdraw {id}`` (4.5): the proposer takes its own proposal back (the lead may withdraw any)."""
    record = _proposal(ctx, op)
    if record.get("author") != ctx.author.name and not is_lead(ctx.author):
        raise C._error("element_not_yours", "{} is {}'s proposal; only its author (or the operator) withdraws it".format(
            record["id"], C._who(record.get("author"), None)), id=record["id"], author=record.get("author"))
    ctx.other("proposal", "update", record["id"], _decided(record, "withdrawn", ctx, None))
    ctx.extra["withdraws"] = record["id"]


# --------------------------------------------------------------------------
# freeze, thaw and settings (5)


def op_freeze(ctx: Any, op: Dict[str, Any]) -> None:
    if not is_lead(ctx.author):
        raise _lead_only("freezing")
    if (op.get("region") is None) == (op.get("ids") is None):
        raise C._invalid("region", "freeze takes region, or ids (exactly one)")
    label = C._text(op.get("label"), "label", C.MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True) or "frozen"
    region: Optional[List[int]] = None
    ids: Optional[List[str]] = None
    if op.get("region") is not None:
        region = C._region(op["region"], "region", ctx.lookup)
    else:
        refs = op["ids"]
        if not isinstance(refs, list) or not refs or len(refs) > C.MAX_ELEMENTS:
            raise C._invalid("ids", "ids is a non-empty list of elements")
        ids = []
        for ref in refs:
            eid = ctx.lookup(ref, "ids")["id"]
            if eid not in ids:
                ids.append(eid)
    xid = ctx.new_id("X")
    ctx.other("freeze", "add", xid, {"id": xid, "kind": "freeze", "region": region, "ids": ids, "label": label, "by": ctx.author.name, "at": ctx.ts})
    ctx.created.append(xid)


def op_thaw(ctx: Any, op: Dict[str, Any]) -> None:
    if not is_lead(ctx.author):
        raise _lead_only("thawing")
    if (op.get("id") is None) == (op.get("ids") is None):
        raise C._invalid("id", "thaw takes id (an X- freeze), or ids (elements to let go of)")
    freezes = ctx.state.freezes
    if op.get("id") is not None:
        xid = op["id"]
        if not isinstance(xid, str) or xid not in freezes:
            raise C._error("element_unknown", "{} is not a freeze".format(xid), ref=xid)
        ctx.other("freeze", "delete", xid, None)
        ctx.changed.append(xid)
        return
    refs = op["ids"]
    if not isinstance(refs, list) or not refs:
        raise C._invalid("ids", "ids is a non-empty list of elements")
    wanted: Set[str] = set()
    for ref in refs:
        if isinstance(ref, str) and C._ELEMENT_ID_RE.match(ref.strip()):
            wanted.add(ref.strip())
        else:
            wanted.add(ctx.lookup(ref, "ids")["id"])
    touched = False
    for xid, freeze in sorted(freezes.items(), key=lambda item: C._id_number(item[0])):
        ids = freeze.get("ids")
        if not isinstance(ids, list) or not wanted & set(ids):
            continue
        left = [i for i in ids if i not in wanted]
        touched = True
        if left:
            ctx.other("freeze", "update", xid, dict(freeze, ids=left))
        else:
            ctx.other("freeze", "delete", xid, None)
        ctx.changed.append(xid)
    if not touched:
        raise C._error("element_unknown", "no freeze names {}".format(C._ids_text(sorted(wanted))), ids=sorted(wanted))


def op_settings(ctx: Any, op: Dict[str, Any]) -> None:
    if not is_lead(ctx.author):
        raise _lead_only("changing the collaboration settings")
    if op.get("human_edits") is None and op.get("frozen") is None:
        raise C._invalid("human_edits", "settings takes human_edits (propose or live) and/or frozen (propose or refuse)")
    current = dict((ctx.state.settings.get(SETTINGS_KEY) or {}))
    if op.get("human_edits") is not None:
        current["human_edits"] = C._choice(op["human_edits"], "human_edits", HUMAN_EDITS, "propose")
    if op.get("frozen") is not None:
        current["frozen"] = C._choice(op["frozen"], "frozen", FROZEN_MODES, "propose")
    ctx.other("setting", "update" if SETTINGS_KEY in ctx.state.settings else "add", SETTINGS_KEY, dict(settings_of_values(current)))
    ctx.entry_extra["settings"] = dict(settings_of_values(current))


def settings_of_values(stored: Mapping[str, Any]) -> Dict[str, str]:
    out = dict(SETTINGS_DEFAULTS)
    for key, allowed in (("human_edits", HUMAN_EDITS), ("frozen", FROZEN_MODES)):
        if stored.get(key) in allowed:
            out[key] = str(stored[key])
    return out


# --------------------------------------------------------------------------
# checkpoints (7)


def checkpoints_dir(team: Any) -> Path:
    return C._dir(team) / CHECKPOINTS_DIR


def checkpoint_path(team: Any, vid: str) -> Path:
    if not isinstance(vid, str) or not C._ID_RE.match(vid) or not vid.startswith("V-"):
        raise C._invalid("id", "{!r} is not a checkpoint id (V-n)".format(vid))
    return checkpoints_dir(team) / (vid + ".json")


def _snapshot(state: Any, vid: str) -> Tuple[bytes, int]:
    elements = sorted(state.elements.values(), key=lambda e: (int(e.get("z") or 0), C._id_number(e.get("id"))))
    data = json.dumps({"v": 1, "id": vid, "version": state.version, "elements": elements}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_CHECKPOINT_BYTES:
        raise C._too_big("elements", "MAX_CHECKPOINT_BYTES", MAX_CHECKPOINT_BYTES, "the canvas is {} KB; a checkpoint holds at most {} KB".format(
            len(data) // 1024, MAX_CHECKPOINT_BYTES // 1024))
    return data, len(elements)


def _prune_checkpoints(ctx: Any, record: Mapping[str, Any]) -> None:
    """Room for ``record``: the lead keeps 30 named, each other author 3, and there are 10 automatic ones (oldest go)."""
    existing = sorted(ctx.state.checkpoints.values(), key=lambda r: C._id_number(r.get("id")))
    if record.get("auto"):
        same = [r for r in existing if r.get("auto")]
        keep = AUTO_CHECKPOINTS
    else:
        same = [r for r in existing if not r.get("auto") and r.get("by") == record.get("by")]
        keep = LEAD_NAMED_CHECKPOINTS if record.get("by") == C.HUMAN else AUTHOR_NAMED_CHECKPOINTS
    while len(same) >= keep:
        oldest = same.pop(0)
        ctx.other("checkpoint", "delete", oldest["id"], None)
        ctx.files[str(oldest["id"])] = None


def _add_checkpoint(ctx: Any, label: str, auto: bool) -> str:
    vid = ctx.new_id("V")
    data, count = _snapshot(ctx.state, vid)
    record = {"id": vid, "label": label, "version": ctx.state.version, "by": ctx.author.name, "at": ctx.ts, "auto": bool(auto), "elements": count}
    _prune_checkpoints(ctx, record)
    ctx.other("checkpoint", "add", vid, record)
    ctx.files[vid] = data
    return vid


def _auto_checkpoint(ctx: Any) -> None:
    batch: Optional[Batch] = getattr(ctx, "collab", None)
    if batch is None or not batch.auto_due:
        return
    reason, batch.auto_due = batch.auto_due, None
    _add_checkpoint(ctx, "before {} ({})".format(ctx.batch_id, reason), True)


def op_checkpoint(ctx: Any, op: Dict[str, Any]) -> None:
    """``checkpoint {label}`` saves the canvas's elements as ``V-n``; ``checkpoint {remove: V-n}`` drops one."""
    if op.get("remove") is not None:
        if op.get("label") is not None:
            raise C._invalid("remove", "checkpoint takes label, or remove")
        vid = op["remove"]
        record = ctx.state.checkpoints.get(vid) if isinstance(vid, str) else None
        if record is None:
            raise C._error("element_unknown", "{} is not a checkpoint".format(vid), ref=vid)
        if record.get("by") != ctx.author.name and not is_lead(ctx.author):
            raise C._error("element_not_yours", "{} is {}'s checkpoint".format(vid, C._who(record.get("by"), None)), id=vid, author=record.get("by"))
        ctx.other("checkpoint", "delete", vid, None)
        ctx.files[vid] = None
        ctx.changed.append(vid)
        return
    label = C._text(op.get("label"), "label", C.MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True, required=True)
    ctx.created.append(_add_checkpoint(ctx, label, False))


def read_checkpoint(team: Any, vid: str) -> Dict[str, Any]:
    path = checkpoint_path(team, vid)
    try:
        size = os.lstat(path).st_size
    except OSError:
        raise C._error("element_unknown", "{}'s snapshot is missing".format(vid), ref=vid)
    if size > MAX_CHECKPOINT_BYTES:
        raise C._too_big("id", "MAX_CHECKPOINT_BYTES", MAX_CHECKPOINT_BYTES, "{} is larger than a checkpoint may be".format(vid))
    doc = store.read_json(path, default=None)
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("elements"), list):
        raise C._error("element_unknown", "{}'s snapshot cannot be read".format(vid), ref=vid)
    return doc


def _unchanged(before: Optional[Mapping[str, Any]], now: Optional[Mapping[str, Any]]) -> bool:
    """Whether a key's value is what it was (both gone, or the same bar the write stamps)."""
    if before is None or now is None:
        return before is None and now is None
    return _same(before, now)


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    skip = ("updated_seq", "updated_at")
    return {k: v for k, v in a.items() if k not in skip} == {k: v for k, v in b.items() if k not in skip}


def op_restore(ctx: Any, op: Dict[str, Any]) -> None:
    """``restore {id: V-n}`` (lead only): an automatic checkpoint of now, then the difference to the snapshot as one op
    (comments are never rolled back; claims, locks, freezes, proposals and settings are untouched)."""
    if not is_lead(ctx.author):
        raise _lead_only("restoring a checkpoint")
    vid = op.get("id")
    if not isinstance(vid, str) or vid not in ctx.state.checkpoints:
        raise C._error("element_unknown", "{} is not a checkpoint".format(vid), ref=vid)
    doc = read_checkpoint(ctx.team, vid)
    _add_checkpoint(ctx, "before {} (restore {})".format(ctx.batch_id, vid), True)
    snap = {el["id"]: el for el in doc["elements"] if isinstance(el, dict) and isinstance(el.get("id"), str) and el.get("type") != "comment"}
    now = {el["id"]: el for el in ctx.live() if el.get("type") != "comment"}
    added = changed = 0
    restored: List[str] = []
    for eid, value in snap.items():
        current = now.get(eid)
        if current is not None and _same(current, value):
            continue
        new = dict(value, updated_seq=ctx.seq, updated_at=ctx.ts)
        if new.get("alias"):
            owner = (ctx.state.aliases.get(new["alias"]) or {}).get(str(new.get("author")))
            if owner is not None and owner != eid and owner not in snap and ctx.el(owner) is not None:
                new["alias"] = None
        ctx.put(new)
        restored.append(eid)
        if current is None:
            added += 1
        else:
            changed += 1
    gone = {eid for eid in now if eid not in snap}
    for eid in sorted(gone, key=C._id_number):
        ctx.drop(eid)
    if gone:
        C._unbind(ctx, ctx.live(), gone)
    C._unbind(ctx, [el for el in (ctx.el(i) for i in restored) if el is not None])
    C._reroute_bound(ctx, restored, skip=restored)
    ctx.extra["restores"] = vid
    ctx.entry_extra["restore"] = {"from": vid, "added": added, "changed": changed, "deleted": len(gone)}


def write_files(ctx: Any) -> None:
    """Write the checkpoint files an applied op staged (under the lock); deletions wait for ``finish_files``."""
    batch: Optional[Batch] = getattr(ctx, "collab", None)
    for vid, data in sorted(ctx.files.items()):
        if data is None:
            if batch is not None:
                batch.deletions.append(vid)
            continue
        path = checkpoint_path(ctx.team, vid)
        check_not_symlink(path)
        store.atomic_write(path, data, fsync=False)
    ctx.files = {}


def finish_files(ctx: Any) -> None:
    """After the batch's events are in the log: delete the checkpoint files it pruned or removed."""
    batch: Optional[Batch] = getattr(ctx, "collab", None)
    for vid in batch.deletions if batch is not None else ():
        try:
            os.unlink(checkpoint_path(ctx.team, vid))
        except OSError:
            pass


_ASSET_NAME_RE = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|svg|html|vl\.json|json|glb)")


def keep_checkpoint_assets(team: Any, records: Mapping[str, Any], archived: Path) -> int:
    """After ``clear`` archived the assets: copy back the ones a kept checkpoint's elements name, so a restore after the
    clear still has its pictures and datasets. The count copied."""
    names: Set[str] = set()
    for vid in records:
        try:
            names.update(_ASSET_NAME_RE.findall(checkpoint_path(team, vid).read_text(encoding="utf-8")))
        except (OSError, HerdrTeamError, ValueError):
            continue
    copied = 0
    for name in sorted(names):
        source = archived / name
        if source.is_file() and not source.is_symlink():
            data = store.read_bytes(source)
            if data is not None:
                store.atomic_write(C._dir(team) / C.ASSETS_DIR / name, data, fsync=False)
                copied += 1
    return copied


def clear_checkpoint(team: Any, state: Any, now: float) -> Dict[str, Any]:
    """``canvas clear``: an automatic checkpoint of the canvas it archives; the records the cleared canvas keeps."""
    records = {vid: dict(record) for vid, record in state.checkpoints.items()}
    number = max(state.counters.get("V", 0), 0) + 1
    vid = "V-{}".format(number)
    data, count = _snapshot(state, vid)
    autos = sorted((r for r in records.values() if r.get("auto")), key=lambda r: C._id_number(r.get("id")))
    while len(autos) >= AUTO_CHECKPOINTS:
        oldest = autos.pop(0)
        records.pop(oldest["id"], None)
        try:
            os.unlink(checkpoint_path(team, oldest["id"]))
        except OSError:
            pass
    path = checkpoint_path(team, vid)
    store.atomic_write(path, data, fsync=False)
    records[vid] = {"id": vid, "label": "before clear", "version": state.version, "by": C.HUMAN, "at": C._iso(now), "auto": True, "elements": count}
    return records


# --------------------------------------------------------------------------
# per-author undo and revert (6)


_UNDO_TARGETS = ("element", "claim", "legend", "setting", "freeze")


def _undo_may(author: Any, value: Optional[Mapping[str, Any]]) -> bool:
    if value is None or is_lead(author):
        return True
    if value.get("author_kind") == C.KIND_HUMAN:
        return False
    return C._may_edit(author, dict(value))


def editing_ids(human: Optional[Mapping[str, Any]]) -> Set[str]:
    """What the operator is editing now, on any of her fresh pages (``editing_all``), or on the freshest (``editing``)."""
    human = human or {}
    found = {i for i in human.get("editing_all") or [] if isinstance(i, str)}
    if isinstance(human.get("editing"), str):
        found.add(human["editing"])
    return found


class _UndoGuard:
    """What binds an undo by anyone but the lead as it binds a direct op (QA phase 5 H1): a freeze leaves its marks as
    they are (skipped, said, and left for a later undo), and the element the operator is editing refuses the undo
    (``element_busy``, retryable). The lead is never guarded."""

    def __init__(self, ctx: Any) -> None:
        state = ctx.state
        self.freezes = tuple(dict(f) for f in state.freezes.values())
        self.frozen = frozen_map(state.elements, self.freezes) if self.freezes else {}
        self.editing = editing_ids(getattr(getattr(ctx, "collab", None), "human", None))

    def check(self, eid: str, current: Optional[Dict[str, Any]], value: Optional[Dict[str, Any]]) -> Optional[str]:
        """The freeze (``X-n``) that holds this write, or None; raises ``element_busy`` for what the operator is editing."""
        if current is None and value is None:
            return None
        if eid in self.editing:
            raise C._error("element_busy", "{} is being edited by the operator right now; try the undo again in a few seconds".format(eid),
                           retry_after_s=BUSY_RETRY_S, id=eid, reason="busy", ids=[eid])
        return self.held(eid, current, value)

    def held(self, eid: str, current: Optional[Dict[str, Any]], value: Optional[Dict[str, Any]]) -> Optional[str]:
        """The freeze (``X-n``) that holds this write, or None (no busy check: for a write skipped anyway)."""
        if not self.freezes or (current is None and value is None):
            return None
        action = "add" if current is None else ("delete" if value is None else "update")
        hits = frozen_hits([Change(eid, action, current, value, True)], self.freezes, self.frozen)
        return hits[0][1] if hits else None


def _left_keys(entry: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """The keys an earlier undo of this batch left as they were (skipped, frozen or not the undoer's to change)."""
    out: List[Tuple[str, str]] = []
    for key in entry.get("left") or []:
        if isinstance(key, list) and len(key) == 2 and key[0] in _UNDO_TARGETS and isinstance(key[1], str):
            out.append((str(key[0]), str(key[1])))
    return out


def _counted(keys: Iterable[Tuple[Any, Any]], basis: Iterable[Tuple[Any, Any]]) -> int:
    """What "N of M reverted" counts (QA phase 5 L3): the marks and settings, not the claims that came with them, unless
    the batches (``basis``) touched only claims."""
    keys = set(keys)
    if any(k[0] != "claim" for k in basis):
        keys = {k for k in keys if k[0] != "claim"}
    return len(keys)


def _open_made(state: Any, by_batch: Mapping[str, List[Dict[str, Any]]], batches: Sequence[str]) -> List[str]:
    """The proposals these batches made that are still open."""
    out: List[str] = []
    for bid in batches:
        for event in by_batch.get(bid) or []:
            for change in event.get("changes") or []:
                pid = change.get("id") if isinstance(change, dict) and change.get("target") == "proposal" and change.get("action") == "add" else None
                record = state.proposals.get(pid) if isinstance(pid, str) else None
                if record is not None and record.get("status") == "open" and pid not in out:
                    out.append(pid)
    return out


def _force_hint(skip: Mapping[str, Any], reader: str, later_batch: Mapping[str, str], lead: bool) -> str:
    """What gets past a skip for an edit made later: the reader's own later batch undone first, or the lead's ``force``."""
    force = "force it with \"force\": true" if lead else "force it with \"force\": true (the operator)"
    if (skip.get("by") or C.HUMAN) == reader and later_batch.get(skip["id"]):
        return "undo {} first, or {}".format(later_batch[skip["id"]], force)
    return force


def _nothing_why(skips: Sequence[Mapping[str, Any]], refused: Sequence[str], reader: str, later_batch: Mapping[str, str], lead: bool) -> str:
    """Why an undo would take nothing back, from its first skip, and what would."""
    more = len(skips) + len(refused) - 1
    tail = " (and {} more)".format(more) if more > 0 else ""
    if not skips:
        return "{} {} not yours to change{}".format(C._ids_text(refused), "is" if len(refused) == 1 else "are", tail) if refused else \
            "it changed nothing that is still there to take back"
    first = skips[0]
    later = "{} later (v{})".format(C._who(first.get("by") or C.HUMAN, reader), first["seq"]) if first.get("seq") is not None else ""
    if first.get("reason") == "frozen":
        text = "{} is frozen ({}){}{}".format(first["id"], first["freeze"], " and was edited by " + later if later else "", tail)
        return text + ("; force it with \"force\": true" if lead else "; the operator may force it (\"force\": true)")
    text = "{} was edited by {}{}".format(first["id"], later, tail)
    if (first.get("by") or C.HUMAN) == reader and later_batch.get(first["id"]):
        return text + "; undo {} first{}".format(later_batch[first["id"]], ", or force it with \"force\": true" if lead else
                                                 ", or the operator may force it (\"force\": true)")
    return text + ("; force it with \"force\": true" if lead else "; the operator may force it (\"force\": true)")


def op_undo(ctx: Any, op: Dict[str, Any]) -> None:
    """``undo {batch}`` or ``undo {author, since}``: write back what the batches changed, skipping every element someone
    outside them changed later (``force``, the lead only, writes back anyway), anything the undoer may not edit, and, for
    anyone but the lead, what a freeze holds. What it leaves is kept on the batch (``left``): ``undo {batch}`` again tries
    those keys once more, and the lead's ``force`` writes them back. Open proposals the batch made are withdrawn."""
    author = ctx.author
    lead = is_lead(author)
    force = C._bool(op.get("force"), "force", False)
    if force and not lead:
        raise _lead_only("forcing an undo over later edits")
    if (op.get("batch") is None) == (op.get("author") is None):
        raise C._invalid("batch", "undo takes batch (B-n), or author (and since)")
    if op.get("since") is not None and op.get("author") is None:
        raise C._invalid("since", "since goes with author")
    state = ctx.state
    events = C._read_events(ctx.team)
    #: A batch an earlier undo left keys of: only those keys are tried again.
    retry: Dict[str, List[Tuple[str, str]]] = {}
    if op.get("batch") is not None:
        bid = op["batch"]
        if not isinstance(bid, str) or not re.match(r"^B-[1-9][0-9]{0,6}\Z", bid.strip()):
            raise C._invalid("batch", "undo needs batch: a B- id")
        bid = bid.strip()
        entry = state.batches.get(bid)
        if entry is None:
            raise C._error("element_unknown", "{} is not in the canvas history (too old, or cleared)".format(bid), ref=bid)
        if entry.get("undone"):
            left = _left_keys(entry)
            if not left:
                raise C._invalid("batch", "{} is already undone".format(bid))
            retry[bid] = left
        batches = [bid]
    else:
        name = op["author"]
        if not isinstance(name, str) or not name.strip():
            raise C._invalid("author", "author names a member, or human")
        name = name.strip()
        since = int(C._num(op["since"], "since", 0, 10 ** 12)) if op.get("since") is not None else None
        allowed = lead or (author.is_member and (name == author.name or (name != C.HUMAN and (author.manager or author.operator))))
        if not allowed:
            raise C._error("operator_only" if name == C.HUMAN else "element_not_yours", "reverting {}'s batches is for {}".format(
                C._who(name, None), "the operator" if name == C.HUMAN else "the manager, a delegate or the operator"), author=name)
        mine = [(bid, e) for bid, e in state.batches.items() if e.get("author") == name and bid != ctx.batch_id
                and (since is None or int(e.get("first_seq") or 0) > since)]
        batches = [bid for bid, e in mine if not e.get("undone")]
        if force:
            # The lead's force also writes back what earlier reverts of these batches left (QA phase 5 M1).
            for bid, e in mine:
                if e.get("undone") and _left_keys(e):
                    batches.append(bid)
                    retry[bid] = _left_keys(e)
        # A revert of an author never redoes what that author took back itself: its own undo batches are not reverted.
        undos = {e.get("batch") for e in events if e.get("op") == "undo"}
        batches = [bid for bid in batches if bid not in undos]
        if not batches:
            raise C._error("element_unknown", "{} has no batch to undo{}".format(C._who(name, None), " since v{}".format(since) if since is not None else ""),
                           author=name, since=since)
    by_batch: Dict[str, List[Dict[str, Any]]] = {}
    undid_by: Dict[str, Set[str]] = {}
    for event in events:
        bid = event.get("batch")
        if isinstance(bid, str) and bid in batches:
            by_batch.setdefault(bid, []).append(event)
        if event.get("op") == "undo" and isinstance(bid, str):
            for gone in ([event.get("undoes")] if isinstance(event.get("undoes"), str) else []) + list(event.get("undoes_all") or []):
                if gone in retry:
                    undid_by.setdefault(gone, set()).add(bid)
    missing = [b for b in batches if b not in by_batch]
    if missing and len(missing) == len(batches):
        raise C._error("element_unknown", "{} is not in the canvas log (cleared)".format(missing[0]), ref=missing[0])
    batches = [b for b in batches if b in by_batch]
    if op.get("batch") is not None:
        first_author = by_batch[batches[0]][0].get("author") if isinstance(by_batch[batches[0]][0].get("author"), dict) else {}
        agent_batch = first_author.get("kind") == C.KIND_MEMBER
        allowed = lead or (author.is_member and agent_batch and (first_author.get("name") == author.name or author.manager or author.operator))
        if not allowed:
            raise C._error("operator_only" if not agent_batch else "element_not_yours", "undoing {} ({}'s batch) is for {}".format(
                batches[0], C._who(first_author.get("name"), None),
                "the operator in person" if not agent_batch else "the manager, a delegate or the operator"),
                batch=batches[0], author=first_author.get("name"))
    ordered = sorted(batches, key=lambda b: -min(int(e["seq"]) for e in by_batch[b]))
    reverting = set(ordered)
    guard = None if lead or not GATE_ON else _UndoGuard(ctx)
    # Every change to every key, in log order: (seq, batch, author name, action, value); a clear starts over.
    history: Dict[Tuple[str, str], List[Tuple[int, Optional[str], str, Optional[Dict[str, Any]]]]] = {}
    for event in events:
        if event.get("op") == "clear":
            history = {key: [] for key in history}
            continue
        who = event.get("author") if isinstance(event.get("author"), dict) else {}
        for change in event.get("changes") or []:
            if not isinstance(change, dict):
                continue
            key = (change.get("target"), change.get("id"))
            if key[0] in _UNDO_TARGETS and isinstance(key[1], str):
                value = None if change.get("action") == "delete" else change.get("value")
                history.setdefault(key, []).append((int(event["seq"]), event.get("batch"), str(who.get("name") or ""), value))  # type: ignore[index]
    writes: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
    order: List[Tuple[str, str]] = []
    skipped: Dict[str, Dict[str, Any]] = {}
    refused: List[str] = []
    left: Dict[str, List[Tuple[str, str]]] = {}
    later_batch: Dict[str, str] = {}
    counted: Set[Tuple[str, str]] = set()
    for bid in ordered:
        seqs = [int(e["seq"]) for e in by_batch[bid]]
        first, last = min(seqs), max(seqs)
        touched: List[Tuple[str, str]] = []
        for event in by_batch[bid]:
            for change in event.get("changes") or []:
                key = (change.get("target"), change.get("id")) if isinstance(change, dict) else (None, None)
                if key[0] in _UNDO_TARGETS and isinstance(key[1], str) and key not in touched:
                    touched.append(key)  # type: ignore[arg-type]
        if bid in retry:
            touched = [key for key in touched if key in retry[bid]]
        # Changes by the undos that already took this batch back are not "someone else's later edit".
        ours = reverting | undid_by.get(bid, set())
        counted.update(touched)
        for key in touched:
            timeline = history.get(key) or []
            others = [entry for entry in timeline if entry[0] > last and entry[1] not in ours]
            # Later changes that cancel out (a newer batch undone since, ours or not: undo twice in a row) leave the key as
            # this batch left it; it is not "edited later". Named: the first later change still in effect, else the first.
            later = None if others and _unchanged(next((e[3] for e in reversed(timeline) if e[0] <= last), None), timeline[-1][3]) else \
                next((e for e in others if not (state.batches.get(e[1]) or {}).get("undone")), others[0] if others else None)
            value = None
            for entry in timeline:
                if entry[0] >= first:
                    break
                value = entry[3]
            if later is not None and not force:
                if key[0] == "claim":
                    # A claim renewed since is bookkeeping: not reported, not something to come back for (QA phase 5 verdict).
                    continue
                skip: Dict[str, Any] = {"id": key[1], "by": later[2] or None, "seq": later[0]}
                # Frozen and edited later: the freeze is named first, it is what an agent cannot get past (QA phase 5 verdict).
                held = guard.held(key[1], ctx.el(key[1]), value) if guard is not None and key[0] == "element" else None
                if held is not None:
                    skip = {"id": key[1], "reason": "frozen", "freeze": held, "by": later[2] or None, "seq": later[0]}
                skipped.setdefault(key[1], skip)
                if isinstance(later[1], str) and not (state.batches.get(later[1]) or {}).get("undone"):
                    later_batch.setdefault(key[1], later[1])
                left.setdefault(bid, []).append(key)
                continue
            if key[0] == "element":
                current = ctx.el(key[1])
                if not (_undo_may(author, current) and _undo_may(author, value)):
                    if (current or value or {}).get("type") == "comment":
                        counted.discard(key)  # someone else's comment followed its element: it follows it back (or is put back on it, below)
                        continue
                    if key[1] not in refused:
                        refused.append(key[1])
                    left.setdefault(bid, []).append(key)
                    continue
                held = guard.check(key[1], current, value) if guard is not None else None
                if held is not None:
                    skipped.setdefault(key[1], {"id": key[1], "reason": "frozen", "freeze": held})
                    left.setdefault(bid, []).append(key)
                    continue
            writes[key] = value
            if key not in order:
                order.append(key)
    if _counted(order, counted) == 0 and not _open_made(state, by_batch, ordered):
        # An undo that would take nothing back is refused, saying why and what would (QA phase 5 verdict): it writes no
        # event, so History does not strike the batch, and ``undo`` again tries the whole batch once more.
        skips = [skipped[k] for k in sorted(skipped, key=lambda i: (C._id_number(i), i))]
        why = _nothing_why(skips, refused, author.name, later_batch, lead)
        field = "batch" if op.get("batch") is not None else "author"
        if retry and all(b in retry for b in ordered):
            keys = retry[ordered[0]]
            raise C._invalid(field, "{} is already undone; it left {} as {}: {}".format(
                ordered[0], C._ids_text([k[1] for k in keys]), "it is" if len(keys) == 1 else "they are", why), skipped=skips, refused=refused)
        raise C._invalid(field, "{} {} not undone, nothing in {} can be taken back now: {}".format(
            C._ids_text(ordered), "was" if len(ordered) == 1 else "were", "it" if len(ordered) == 1 else "them", why), skipped=skips, refused=refused)
    boxes = []
    restored: List[str] = []
    dropped: Set[str] = set()
    for key in order:
        target, ident = key
        value = writes[key]
        if target == "element":
            current = ctx.el(ident)
            if current is not None:
                boxes.append(C.bounds(current))
            if value is None:
                if current is not None:
                    ctx.drop(ident)
                    dropped.add(ident)
                continue
            new = dict(value, updated_seq=ctx.seq, updated_at=ctx.ts)
            if new.get("alias"):
                owner = (state.aliases.get(new["alias"]) or {}).get(str(new.get("author")))
                if owner is not None and owner != ident and ctx.el(owner) is not None:
                    new["alias"] = None
            ctx.put(new)
            boxes.append(C.bounds(new))
            restored.append(ident)
            continue
        store_map = {"claim": state.claims, "legend": state.legend, "setting": state.settings, "freeze": state.freezes}[target]
        current_other = store_map.get(ident)
        if value is None:
            if current_other is not None:
                ctx.other(target, "delete", ident, None)
                ctx.changed.append(ident)
        elif not (target == "claim" and C._expired(value, ctx.now)):
            ctx.other(target, "update" if current_other is not None else "add", ident, value)
            if target != "setting":
                ctx.changed.append(ident)
    ctx.undo_restored.update(restored)
    if dropped:
        C._unbind(ctx, ctx.live(), dropped)
    C._unbind(ctx, [el for el in (ctx.el(ident) for ident in restored) if el is not None])
    back = set(restored)
    if back:
        # A comment a delete left behind (``was_on``) is on its element again once the element is back (QA phase 5 L2).
        for el in list(ctx.live()):
            if el.get("type") == "comment" and not el.get("on") and el.get("was_on") in back:
                again = dict(el, on=el["was_on"], updated_seq=ctx.seq, updated_at=ctx.ts)
                again.pop("was_on", None)
                ctx.put(again)
    C._check_locks(ctx, boxes)
    C._reroute_bound(ctx, restored, skip=restored)
    # An open proposal the batch made goes with it (QA phase 5 L4): withdrawn, as its author or the lead would.
    withdrew: List[str] = []
    for pid in _open_made(state, by_batch, ordered):
        record = state.proposals[pid]
        if record.get("author") == author.name or lead:
            ctx.other("proposal", "update", pid, _decided(record, "withdrawn", ctx, "undone with {}".format(record.get("batch"))))
            withdrew.append(pid)
    of = _counted(counted, counted)
    done = _counted(order, counted)
    skips = [skipped[k] for k in sorted(skipped, key=lambda i: (C._id_number(i), i))]
    later_skips = [s for s in skips if s.get("reason") is None]
    frozen_skips = [s for s in skips if s.get("reason") == "frozen"]
    if later_skips:
        first_skip = later_skips[0]
        ctx.warn("undo_skipped", "{} of {} reverted; {} edited by {} later (v{}){}: {}".format(
            done, of, first_skip["id"], C._who(first_skip["by"] or C.HUMAN, author.name), first_skip["seq"],
            " and {} more".format(len(later_skips) - 1) if len(later_skips) > 1 else "", _force_hint(first_skip, author.name, later_batch, lead)),
            [s["id"] for s in later_skips])
    if frozen_skips:
        ids = [s["id"] for s in frozen_skips]
        also = next((s for s in frozen_skips if s.get("seq") is not None), None)
        ctx.warn("undo_skipped", "{} of {} reverted; {} {} frozen ({}){}: the operator holds {} as {} {}; ask her in a comment".format(
            done, of, C._ids_text(ids), "is" if len(ids) == 1 else "are", ", ".join(dict.fromkeys(s["freeze"] for s in frozen_skips)),
            "; {} was also edited by {} later (v{})".format(also["id"], C._who(also["by"] or C.HUMAN, author.name), also["seq"]) if also else "",
            "it" if len(ids) == 1 else "them", "it" if len(ids) == 1 else "they", "is" if len(ids) == 1 else "are"), ids)
    if refused:
        ctx.warn("undo_skipped", "undo left {} as {} now: changing {} is for its editor".format(
            C._ids_text(refused), "it is" if len(refused) == 1 else "they are", "it" if len(refused) == 1 else "them"), refused)
    if len(ordered) == 1:
        ctx.extra["undoes"] = ordered[0]
    else:
        ctx.extra["undoes_all"] = list(ordered)
    if left:
        ctx.extra["undo_left"] = {bid: [list(key) for key in keys] for bid, keys in sorted(left.items(), key=lambda item: C._id_number(item[0]))}
    ctx.entry_extra["undo"] = {"batches": list(ordered), "restored": done, "of": of, "skipped": skips}
    if withdrew:
        ctx.entry_extra["undo"]["withdrew"] = withdrew


# --------------------------------------------------------------------------
# the display list (4.6, 5.2)


def _ghost(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """An entry's items at half their opacity; browser-drawn slots become their drawing (or a dashed outline)."""
    out: List[Dict[str, Any]] = []
    for item in items:
        item = dict(item)
        if item.get("k") == "slot":
            fallback = item.get("fallback") if item.get("drawn") and isinstance(item.get("fallback"), list) else None
            if fallback:
                item = {"k": "group", "items": list(fallback)}
            else:
                item = {"k": "rect", "x": item.get("x", 0), "y": item.get("y", 0), "w": item.get("w", 1), "h": item.get("h", 1), "fill": None,
                        "stroke": "tone.neutral.stroke", "sw_px": 1.5, "dash": [8, 6]}
        opacity = item.get("op")
        item["op"] = D.r2((float(opacity) if isinstance(opacity, (int, float)) else 1.0) * GHOST_OPACITY)
        out.append(item)
    return out


#: A proposed element's ghost items, by what they depend on: an open proposal's values never change, so a full rebuild of
#: the display list (every new version the delta cannot patch) draws each ghost once per process, not once per rebuild.
_GHOSTS: Dict[Tuple[str, str, str], Tuple[List[Dict[str, Any]], Tuple[float, ...]]] = {}
MAX_GHOSTS = 1000


def _ghost_of(value: Mapping[str, Any], env: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], Tuple[float, ...]]:
    from herdr_team import canvas_text as _ctext

    active = _ctext._CORRECTIONS.get()
    key = (hashlib.sha1(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")).hexdigest(),
           active[0] if active else "", json.dumps(D.chip(value.get("author"), dict(env)), sort_keys=True))
    hit = _GHOSTS.get(key)
    if hit is None:
        found = D.entry(value, dict(env))
        if len(_GHOSTS) >= MAX_GHOSTS:
            _GHOSTS.clear()
        hit = _GHOSTS[key] = (_ghost(found.get("items") or []), tuple(found["bbox"]))
    return hit


def _aside_shift(record: Mapping[str, Any], elements: Mapping[str, Mapping[str, Any]]) -> float:
    """How far right an in-place ghost is drawn from its host, or 0 to draw it where it lands (QA phase 6, F3).

    A proposal that rewrites a mark without moving it used to ghost it at the host's own origin: the ghost's words ran
    through the host's, and the two author chips sat on each other, so neither could be read. Such a ghost is drawn
    beside its host instead, with the host still visible for comparison. A proposal that keeps the origin but grows the
    mark counts as in place too, since that is what a longer label does. Every other proposal (an addition, a move, a
    delete) keeps drawing where the change lands, because there the position *is* the proposal.

    The shift is only taken when it really moves every ghosted box - an arrow, a pen stroke or a path is drawn from its
    own points, so shifting its origin would leave it where it was, and those keep their in-place ghost.
    """
    values: List[Tuple[Mapping[str, Any], Box]] = []
    for change in record.get("changes") or []:
        if not isinstance(change, dict) or change.get("action") == "delete":
            return 0.0
        value = change.get("value") if isinstance(change.get("value"), dict) else None
        current = elements.get(change.get("id")) if isinstance(change.get("id"), str) else None
        if value is None or current is None or change.get("action") != "update":
            return 0.0
        here, there = D.box_of(current), D.box_of(value)
        if (here[0], here[1]) != (there[0], there[1]):
            return 0.0  # the proposal moves it: the ghost belongs where it lands
        values.append((value, there))
    if not values:
        return 0.0
    x0 = min(box[0] for _value, box in values)
    x1 = max(box[2] for _value, box in values)
    shift = C._r2(x1 - x0 + GHOST_ASIDE_GAP)
    for value, box in values:
        moved = D.box_of(dict(value, x=C._r2(float(value.get("x") or 0) + shift)))
        if abs(moved[0] - (box[0] + shift)) > 0.51:
            return 0.0  # its box does not follow its origin (points, a route): leave it in place
    return shift


def proposal_entry(record: Mapping[str, Any], elements: Mapping[str, Mapping[str, Any]], env: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """One open proposal's ghost (4.6): the proposed elements' own primitives at half opacity, a dashed line for each
    move, a dashed box for each delete, an outline around it all and a label pill, in the author's chip colours."""
    if record.get("status") != "open":
        return None
    stale = outdated_of(record, elements)
    items: List[Dict[str, Any]] = []
    moves: List[Dict[str, Any]] = []
    deletes: List[Dict[str, Any]] = []
    boxes: List[Box] = []
    aside = _aside_shift(record, elements)
    for change in record.get("changes") or []:
        if not isinstance(change, dict):
            continue
        value = change.get("value") if isinstance(change.get("value"), dict) else None
        current = elements.get(change.get("id")) if isinstance(change.get("id"), str) else None
        if change.get("action") == "delete":
            if current is not None:
                x0, y0, x1, y1 = D.box_of(current)
                deletes.append({"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "fill": None, "stroke": "tone.danger.stroke",
                                "sw": 2, "dash": [6, 4]})
                boxes.append((x0, y0, x1, y1))
            continue
        if value is None:
            continue
        if aside:
            value = dict(value, x=C._r2(float(value.get("x") or 0) + aside), y=value.get("y"))
        ghost, bbox = _ghost_of(value, env)
        items.extend(ghost)
        boxes.append(bbox)  # type: ignore[arg-type]
        if change.get("action") == "update" and current is not None:
            old, new = D.box_of(current), D.box_of(value)
            if aside:
                # The leader ties the ghost to the host it rewrites: edge to edge, so it crosses neither box.
                moves.append({"k": "line", "points": [[D.r2(old[2]), D.r2((old[1] + old[3]) / 2)],
                                                      [D.r2(new[0]), D.r2((new[1] + new[3]) / 2)]],
                              "stroke": "tone.accent.stroke", "sw_px": 1, "dash": [6, 4], "op": 0.6})
            elif (old[0], old[1]) != (new[0], new[1]) and value.get("type") not in ("arrow", "comment"):
                moves.append({"k": "line", "points": [[D.r2((old[0] + old[2]) / 2), D.r2((old[1] + old[3]) / 2)],
                                                      [D.r2((new[0] + new[2]) / 2), D.r2((new[1] + new[3]) / 2)]],
                              "stroke": "tone.accent.stroke", "sw_px": 1, "dash": [6, 4], "op": 0.6})
    if not boxes:
        return None
    x0, y0 = min(b[0] for b in boxes) - 6, min(b[1] for b in boxes) - 6
    x1, y1 = max(b[2] for b in boxes) + 6, max(b[3] for b in boxes) + 6
    # The entry's bbox also holds where each moved element comes from (its line starts at the old centre).
    ends = [point for line in moves for point in line["points"]]
    bx0, by0 = min([x0] + [p[0] for p in ends]), min([y0] + [p[1] for p in ends])
    bx1, by1 = max([x1] + [p[0] for p in ends]), max([y1] + [p[1] for p in ends])
    author = str(record.get("author") or "")
    chip = D.chip(author, dict(env))
    outline = {"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": 6, "fill": None,
               "stroke": "tone.warning.stroke" if stale else "tone.accent.stroke", "sw_px": 1.5, "dash": [6, 4]}
    intent = " ".join(str(record.get("intent") or "").split())
    if len(intent) > 48:
        intent = intent[:47].rstrip() + "…"
    text = "{} · {} suggests: {}{}".format(record.get("id"), "the operator" if author == C.HUMAN else author, intent, " (outdated)" if stale else "")
    size = 12
    from herdr_team import canvas_text as _ctext  # the label's width is measured like every other label

    width = _ctext.measure(text, "normal", size, 500).width
    pill_w, pill_h = D.r2(width + 16), 20
    pill = {"k": "group", "screen": [x0, y0], "lod": [0.35, None], "items": [
        {"k": "rect", "x": 0, "y": -pill_h - 4, "w": pill_w, "h": pill_h, "r": 10, "fill": chip["bg"], "stroke": None},
        D.text_prim([text], 8, -pill_h - 4 + 14 - _ctext.baseline(size, "normal", 500), size, {}, chip["fg"], "start", None, 500)]}
    record_view = {"author": author, "intent": str(record.get("intent") or ""), "reason": record.get("reason"),
                   "reasons": list(record.get("reasons") or []), "outdated": bool(stale), "targets": list(record.get("targets") or []),
                   "created": list(record.get("created") or []), "deleted": list(record.get("deleted") or []), "batch": record.get("batch"),
                   "summary": list(record.get("summary") or []), "base_note": list(record.get("base_note") or [])}
    return D._rounded({
        "id": str(record.get("id")), "kind": "proposal", "layer": "overlays", "z": PROPOSAL_Z + C._id_number(record.get("id")),
        "v": int(record.get("seq") or 0), "bbox": [bx0, by0, bx1, by1], "hit": {"shape": "rect", "box": [x0, y0, x1 - x0, y1 - y0]},
        "handles": "none", "connect": False, "edit": None, "frame": None, "author": author, "chip": chip, "locked": False,
        "proposal": record_view, "items": items + moves + deletes + [outline, pill]})


def freeze_entry(freeze: Mapping[str, Any], elements: Mapping[str, Mapping[str, Any]], env: Mapping[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    """A freeze's outline (5.2): a dashed rect over its region, or around its elements as they stand now, and its label."""
    box = freeze_box(freeze, elements)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    xid = str(freeze.get("id") or "")
    text = "{} frozen: {}".format(xid, freeze.get("label") or "frozen")[:120]
    from herdr_team import canvas_text as _ctext

    # One outline round the lot, or, for an id freeze whose marks drifted apart, one round each (QA phase 5 L9). The label
    # sits above the top-right corner, right-aligned: it reads as this freeze's caption, clear of a frozen frame's title
    # and a claim's label (both top-left), and the page's label hit is there.
    parts = [box]
    if not (isinstance(freeze.get("region"), list) and len(freeze["region"]) == 4):
        each = [C.bounds(dict(elements[i])) for i in freeze.get("ids") or [] if isinstance(i, str) and i in elements]
        area = lambda b: max(1.0, (b[2] - b[0] + 8) * (b[3] - b[1] + 8))  # noqa: E731
        if len(each) > 1 and area(box) > FREEZE_SPLIT * sum(area(b) for b in each):
            parts = each
    items: List[Dict[str, Any]] = [{"k": "rect", "x": b[0] - 4, "y": b[1] - 4, "w": b[2] - b[0] + 8, "h": b[3] - b[1] + 8, "fill": None,
                                    "stroke": "tone.info.stroke", "sw_px": 1.5, "dash": [10, 6]} for b in parts]
    items.append({"k": "group", "screen": [x1 + 4, y0 - 4], "items": [D.text_prim([text], -4, -6 - _ctext.baseline(12, "normal", 400), 12, {},
                                                                              "tone.info.text", "end", None, 400)]})
    by = str(freeze.get("by") or C.HUMAN)
    ids = [i for i in freeze.get("ids") or [] if isinstance(i, str)] if isinstance(freeze.get("ids"), list) else None
    return D._rounded({
        "id": xid, "kind": "freeze", "layer": "overlays", "z": FREEZE_Z, "v": 0, "bbox": [x0 - 4, y0 - 4, x1 + 4, y1 + 4],
        "hit": {"shape": "frame", "box": [x0 - 4, y0 - 4, x1 - x0 + 8, y1 - y0 + 8], "band": FREEZE_BAND}, "handles": "none", "connect": False,
        "edit": None, "frame": None, "author": by, "chip": D.chip(by, dict(env)), "locked": False,
        "freeze": {"region": list(freeze["region"]) if isinstance(freeze.get("region"), list) else None, "ids": ids,
                   "label": str(freeze.get("label") or ""), "mode": mode}, "items": items})


def display_entries(scene: Mapping[str, Any], env: Mapping[str, Any], wanted: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """The proposal and freeze entries of a scene (``canvas_display.entries`` adds them after locks and claims)."""
    elements = {el["id"]: el for el in scene.get("elements") or [] if isinstance(el, dict) and isinstance(el.get("id"), str)}
    out: List[Dict[str, Any]] = []
    mode = (((scene.get("settings") or {}).get(SETTINGS_KEY) or {}).get("frozen")) or SETTINGS_DEFAULTS["frozen"]
    locks = {lock.get("id") for lock in scene.get("locks") or [] if isinstance(lock, dict)}
    for freeze in scene.get("freezes") or []:
        if isinstance(freeze, dict) and (wanted is None or freeze.get("id") in wanted) and freeze.get("id") not in locks:
            found = freeze_entry(freeze, elements, env, mode)
            if found is not None:
                out.append(found)
    for record in scene.get("proposals") or []:
        if isinstance(record, dict) and (wanted is None or record.get("id") in wanted):
            found = proposal_entry(record, elements, env)
            if found is not None:
                out.append(found)
    return out


def element_flags(scene: Mapping[str, Any]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """``(frozen, pending)``: element id -> its freeze, and element id -> the open proposals aimed at it."""
    elements = {el["id"]: el for el in scene.get("elements") or [] if isinstance(el, dict) and isinstance(el.get("id"), str)}
    frozen = frozen_map(elements, [f for f in scene.get("freezes") or [] if isinstance(f, dict)])
    pending: Dict[str, List[str]] = {}
    for record in scene.get("proposals") or []:
        if isinstance(record, dict) and record.get("status") == "open":
            for eid in record.get("targets") or []:
                if isinstance(eid, str):
                    pending.setdefault(eid, []).append(str(record.get("id")))
    return frozen, pending


# --------------------------------------------------------------------------
# readback (10.1)


def _age(at: Any, now: float) -> str:
    moment = C._parse_iso(at)
    if moment is None:
        return "?"
    seconds = max(0, int(now - moment))
    return "{}s ago".format(seconds) if seconds < 60 else "{}m ago".format(seconds // 60)


def proposal_line(record: Mapping[str, Any], reader: Optional[str], elements: Mapping[str, Mapping[str, Any]]) -> str:
    """``P-3 by you: move E-4 (the operator's), waiting``."""
    targets = [t for t in record.get("targets") or [] if isinstance(t, str)]
    what = "{} {}".format(record.get("op"), C._ids_text(targets) if targets else C._ids_text(record.get("created") or []) or "").strip()
    whose = ""
    if targets:
        # Every target's owner, not only the first's (QA phase 5 L5): "(yours and the operator's)".
        owners = list(dict.fromkeys(_whose(elements[t], reader) for t in targets if t in elements))
        whose = " ({})".format(" and ".join(owners)) if owners else ""
    state = "outdated" if outdated_of(record, elements) else "waiting"
    return "{} by {}: {}{}, {}".format(record.get("id"), C._who(record.get("author"), reader), what, whose, state)


def decided_line(record: Mapping[str, Any], reader: Optional[str]) -> str:
    by = record.get("decided_by")
    text = "{} {} by {}".format(record.get("id"), record.get("status"), C._who(by, reader) if by else "?")
    if record.get("note"):
        text += ": " + C._q(record["note"], 80)
    return text


def operator_line(view: Mapping[str, Any]) -> str:
    """``operator: viewing c10r4:c60r30 · selected E-4, E-7 · editing E-4 · pointing at E-9 · 4s ago`` (9.2)."""
    parts = []
    if isinstance(view.get("viewport"), list) and len(view["viewport"]) == 4:
        parts.append("viewing " + C.region_cells(view["viewport"]))
    if view.get("selection"):
        parts.append("selected " + C._ids_text(view["selection"]))
    if view.get("editing"):
        parts.append("editing {}".format(view["editing"]))
    if view.get("pointing_at"):
        parts.append("pointing at {}".format(view["pointing_at"]))
    age = int(view.get("age_s") or 0)
    parts.append("{}s ago".format(age) if age < 60 else "{}m ago".format(age // 60))
    return "operator: " + " · ".join(parts)


def presence_lines(presence: Mapping[str, Any], reader: Optional[str], now: float) -> List[str]:
    lines: List[str] = []
    human = presence.get("operator")
    if isinstance(human, dict) and reader != C.HUMAN:
        lines.append(operator_line(human))
    members = [m for m in presence.get("members") or [] if isinstance(m, dict) and m.get("name") != reader]
    if members:
        shown = []
        for member in members:
            where = " " + C.region_cells(member["region"]) if isinstance(member.get("region"), list) and len(member["region"]) == 4 else ""
            intent = " " + C._q(member["intent"], 60) if member.get("intent") else ""
            shown.append("{} {}{}{} ({})".format(member.get("name"), member.get("status") or "here", where, intent, _age(member.get("at"), now)))
        lines.append("here: " + "; ".join(shown))
    return lines


def look_lines(result: Mapping[str, Any]) -> List[str]:
    """The collaboration lines of ``look`` (after ``locks:``), each only when it has something to say."""
    reader = result.get("reader")
    now = float(result.get("_now") or time.time())
    elements = result.get("_by_id") or {}
    lines = presence_lines(result.get("presence") or {}, reader, now)
    proposals = result.get("proposals") or []
    if proposals:
        lines.append("proposals ({} open): {}".format(len(proposals), "; ".join(proposal_line(p, reader, elements) for p in proposals)))
    decided = result.get("decided") or []
    if decided:
        lines.append("decided: " + "; ".join(decided_line(p, reader) for p in decided))
    settings = result.get("settings") or SETTINGS_DEFAULTS
    freezes = result.get("freezes") or []
    if freezes:
        mode = "your changes become proposals" if settings.get("frozen") != "refuse" else "refused"
        shown = []
        for freeze in freezes:
            where = C.region_cells(freeze["region"]) if isinstance(freeze.get("region"), list) else C._ids_text(freeze.get("ids") or [])
            shown.append("{} {} {} ({})".format(freeze.get("id"), where, C._q(freeze.get("label") or "", 60), "yours to change" if reader == C.HUMAN else mode))
        lines.append("frozen: " + "; ".join(shown))
    if proposals or freezes or dict(settings) != SETTINGS_DEFAULTS:
        marks = "your marks" if reader == C.HUMAN else "the operator's marks"
        lines.append("settings: {}: {} · frozen: {}".format(marks, "proposals" if settings.get("human_edits") != "live" else "live with revert",
                                                             "proposals" if settings.get("frozen") != "refuse" else "refused"))
    checkpoints = result.get("checkpoints") or []
    if checkpoints:
        lines.append("checkpoints: " + "; ".join("{} {} v{}".format(c.get("id"), "auto" if c.get("auto") else C._q(c.get("label") or "", 60), c.get("version"))
                                                 for c in checkpoints))
    return lines


def proposals_full(result: Mapping[str, Any]) -> List[str]:
    """``look --proposals``: every open proposal in full, its summary, its base note and, when outdated, what changed."""
    reader = result.get("reader")
    elements = result.get("_by_id") or {}
    lines: List[str] = []
    for record in result.get("proposals") or []:
        lines.append("{} — {}".format(proposal_line(record, reader, elements), C._q(record.get("intent") or "", 120)))
        lines += ["  " + line for line in record.get("summary") or []]
        if record.get("base_note"):
            lines.append("  made against v{}; the operator changed since: {}".format(record.get("base"), "; ".join(record["base_note"])))
        team = result.get("_team")
        for entry in (outdated_details(team, record, elements) if team is not None else outdated_of(record, elements)):
            lines.append("  outdated: {}: {}".format(entry.get("id"), "; ".join(entry.get("lines") or [])))
    return lines


def tags(scene: Mapping[str, Any]) -> Dict[str, str]:
    """`` [frozen]`` and `` [proposal P-3]`` for the element lines of ``look``."""
    frozen, pending = element_flags(scene)
    out: Dict[str, str] = {}
    for eid in set(frozen) | set(pending):
        text = ""
        if eid in frozen:
            text += " [frozen]"
        for pid in pending.get(eid, []):
            text += " [proposal {}]".format(pid)
        out[eid] = text
    return out


def answered(elements: Sequence[Mapping[str, Any]]) -> Set[str]:
    """Open comments with a reply by a member they mention (8.3): derived in ``look``, never stored."""
    replies: Dict[str, Set[str]] = {}
    for el in elements:
        if el.get("type") == "comment" and isinstance(el.get("reply_to"), str):
            replies.setdefault(el["reply_to"], set()).add(str(el.get("author")))
    return {str(el["id"]) for el in elements if el.get("type") == "comment" and not el.get("resolved")
            and set(el.get("mentions") or []) & replies.get(str(el.get("id")), set())}


# --------------------------------------------------------------------------
# fixtures the page's tests read (``--write-fixtures`` / ``--check-fixtures``)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "collab"

_BOX = {"id": "E-4", "type": "box", "x": 100, "y": 40, "w": 160, "h": 80, "text": "Pricing", "frame": None,
        "style": {"tone": "neutral", "variant": "soft", "stroke": "#495057", "fill": "#f8f9fa", "width": 2, "dash": "solid"}}

#: The ``diff_lines`` vectors both sides may rely on: ``(name, before, after, parts)``.
DIFF_CASES: Tuple[Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int], ...] = (
    ("added", None, dict(_BOX, frame="E-2"), 0),
    ("added in a cell", None, dict(_BOX, text=""), 0),
    ("deleted", _BOX, None, 0),
    ("moved by grid steps", _BOX, dict(_BOX, x=140, y=100), 0),
    ("moved off the grid", _BOX, dict(_BOX, x=115, y=37.5), 0),
    ("resized", _BOX, dict(_BOX, w=200, h=120), 0),
    ("text", _BOX, dict(_BOX, text="Plans and prices"), 0),
    ("long text", _BOX, dict(_BOX, text="A heading far longer than forty characters, cut with an ellipsis"), 0),
    ("tone", _BOX, dict(_BOX, style=dict(_BOX["style"], tone="danger")), 0),
    ("styles", _BOX, dict(_BOX, style=dict(_BOX["style"], variant="solid", stroke="#e03131", fill=None, width=4, dash="dashed")), 0),
    ("route", dict(_BOX, type="arrow", style={"route": "straight"}), dict(_BOX, type="arrow", style={"route": "orthogonal"}), 0),
    ("into a frame", _BOX, dict(_BOX, frame="E-2"), 0),
    ("out of a frame", dict(_BOX, frame="E-2"), _BOX, 0),
    ("pinned", _BOX, dict(_BOX, pin={"by": "agent", "who": "alpha"}), 0),
    ("unpinned", dict(_BOX, pin={"by": "human", "who": "human"}), _BOX, 0),
    ("block settings", dict(_BOX, type="frame", settings={"layout": "row"}), dict(_BOX, type="frame", settings={"layout": "column", "gap": "l"}), 0),
    ("block parts", dict(_BOX, type="frame"), dict(_BOX, type="frame"), 3),
    ("everything", _BOX, dict(_BOX, x=120, y=60, w=180, text="Plans", frame="E-2", style=dict(_BOX["style"], tone="info"),
                              pin={"by": "agent", "who": "alpha"}), 1),
    ("nothing", _BOX, dict(_BOX), 0),
    # QA phase 6 F3: a card's body, status and owner are words the reader sees, and a rewrite that keeps the opening
    # is quoted from where the two stop sharing, so the line is not spent on the part that did not change.
    ("body rewritten", dict(_BOX, type="card", body="- store credit only after 30 days\n- no cash back"),
     dict(_BOX, type="card", w=320, h=200, body="- store credit only after 45 days\n- no cash back\n- damaged goods refunded"), 0),
    ("body added", dict(_BOX, type="card"), dict(_BOX, type="card", body="Ask the vendor first"), 0),
    ("owner and status", dict(_BOX, type="card", owner="ana", status="todo"), dict(_BOX, type="card", owner="ben", status="doing"), 0),
    ("rows added", dict(_BOX, type="table", rows=[{"id": "r1"}, {"id": "r2"}]),
     dict(_BOX, type="table", rows=[{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]), 0),
    ("a cell rewritten", dict(_BOX, type="table", rows=[{"id": "r1", "cells": {"c1": "EMEA"}}]),
     dict(_BOX, type="table", rows=[{"id": "r1", "cells": {"c1": "EMEA and UK"}}]), 0),
)


def _json_text(doc: Any) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def diff_vectors_text() -> str:
    vectors = [{"name": name, "before": before, "after": after, "parts": parts, "lines": diff_lines(before, after, parts)}
               for name, before, after, parts in DIFF_CASES]
    return _json_text({"about": "canvas_collab.diff_lines(before, after, parts) -> lines (canvas v2 phase 5, 3.4); "
                                "python3 -m herdr_team.canvas_collab --write-fixtures", "vectors": vectors})


#: The moment the fixtures are made at (so presence ages and ISO times are the same on every run).
FIXTURE_NOW = 1790000000.0


def _qa() -> Any:
    import sys

    tools = str(Path(__file__).resolve().parent.parent / "tools")
    sys.path.insert(0, tools)
    try:
        import canvas_qa  # noqa: WPS433 - the QA tool owns the throwaway team, as for the display goldens
    finally:
        sys.path.remove(tools)
    return canvas_qa


def _result_fixtures() -> Dict[str, Any]:
    """One apply result per shape the page handles (I-1), made on a throwaway team at ``FIXTURE_NOW``."""
    from herdr_team import canvas_presence as P

    qa_mod = _qa()
    out: Dict[str, Any] = {}
    now = FIXTURE_NOW
    with qa_mod.QaTeam() as qa:
        drawer, peer, lead = qa.author_for("drawer"), qa.author_for("peer"), qa.author_for("lead")

        def run(ops: List[Dict[str, Any]], author: Any, **kw: Any) -> Dict[str, Any]:
            nonlocal now
            now += 1.0
            return C.apply_ops(qa.layout, qa.team, ops, author, now=now, **kw)

        run([{"op": "shape", "id": "pricing", "kind": "box", "text": "Pricing", "at": "c0r0"},
             {"op": "shape", "id": "faq", "kind": "box", "text": "FAQ", "at": "c0r10"}], lead)
        out["proposed"] = run([{"op": "move", "id": "E-1", "by": [40, 0], "intent": "align the price box"},
                               {"op": "shape", "id": "mine", "kind": "box", "text": "Mine", "at": "c40r0", "intent": "my box"}], drawer)
        out["auto_claim"] = run([{"op": "shape", "kind": "note", "text": "Idea", "at": "c80r0", "intent": "an idea"}], drawer)
        base = C.current_version(qa.team)
        run([{"op": "edit", "id": "E-3", "text": "Mine, edited"}], lead)
        out["stale_base_refused"] = run([{"op": "move", "id": "E-3", "by": [0, 20], "intent": "t"}], drawer, base=base)
        base = C.current_version(qa.team)
        run([{"op": "restyle", "id": "E-3", "tone": "info", "intent": "t"}], qa.author_for("deputy"))
        out["stale_base_warning"] = run([{"op": "move", "id": "E-3", "by": [0, 20], "intent": "t"}], drawer, base=base)
        P.write_human(qa.team, "0123456789abcdef", {"viewport": [0, 0, 1200, 800], "selection": ["E-2"], "editing": "E-3", "cursor": [20, 220]}, now)
        out["element_busy"] = run([{"op": "edit", "id": "E-3", "text": "x", "intent": "t"}], drawer)
        P.write_human(qa.team, "0123456789abcdef", {"viewport": [0, 0, 1200, 800], "selection": ["E-3"], "cursor": [20, 220]}, now)
        out["operator"] = run([{"op": "restyle", "id": "E-3", "tone": "success", "intent": "t"}], drawer)
        out["in_proposal"] = run([{"op": "shape", "id": "held", "text": "On yours", "at": "c1r1", "intent": "t"},
                                  {"op": "arrow", "from": "held", "to": "E-3", "intent": "t"}], drawer)
        run([{"op": "freeze", "ids": ["E-2"], "label": "final"}, {"op": "settings", "frozen": "refuse"}], lead)
        out["frozen"] = run([{"op": "move", "id": "E-2", "by": [0, 20], "intent": "t"}], qa.author_for("deputy"))
        run([{"op": "move", "id": "E-1", "by": [0, 20]}], lead)
        out["proposal_outdated"] = run([{"op": "accept", "id": "P-1"}], lead)
        out["operator_only"] = run([{"op": "accept", "id": "P-1", "intent": "t"}], qa.author_for("deputy"))
        out["accept"] = run([{"op": "accept", "id": out["in_proposal"]["proposed"][0]["proposal"], "note": "fine"}], lead)
        first = run([{"op": "move", "id": "E-3", "by": [20, 0], "intent": "t"}], drawer)["batch"]
        run([{"op": "move", "id": "E-3", "by": [0, 20], "intent": "t"}], drawer)
        run([{"op": "restyle", "id": "E-3", "tone": "danger"}], lead)
        out["undo"] = run([{"op": "undo", "author": qa_mod.MEMBER, "since": 0, "intent": "revert mine"}], lead)
        del first
        out["checkpoint"] = run([{"op": "checkpoint", "label": "before the rework"}], lead)
        run([{"op": "shape", "text": "Later", "at": "c0r40"}], lead)
        out["restore"] = run([{"op": "restore", "id": "V-1"}], lead)
        terms = run([{"op": "shape", "text": "Terms", "at": "c0r60"}], lead)["applied"][0]["ids"][0]
        with mock_limit("MAX_OPEN_PROPOSALS_PER_AUTHOR", 1):
            run([{"op": "move", "id": "E-1", "by": [0, 40], "intent": "t"}], peer)
            out["proposal_limit"] = run([{"op": "move", "id": terms, "by": [0, 40], "intent": "t"}], peer)
        run([{"op": "settings", "human_edits": "live"}], lead)
        out["live_touched"] = run([{"op": "restyle", "id": terms, "tone": "info", "intent": "a calmer tone"}], drawer)
        run([{"op": "thaw", "ids": ["E-2"]}], lead)
        theirs = run([{"op": "shape", "text": "Peer's", "at": "c160r0", "intent": "t"}], peer)["applied"][0]["ids"][0]
        rejected = run([{"op": "move", "id": theirs, "by": [0, 20], "intent": "t"}], drawer)["proposed"][0]["proposal"]
        run([{"op": "reject", "id": rejected, "note": "leave beta's work"}], lead)
        withdrawn = run([{"op": "move", "id": theirs, "by": [0, 40], "intent": "t"}], drawer)["proposed"][0]["proposal"]
        run([{"op": "withdraw", "id": withdrawn, "intent": "not needed"}], drawer)
        keys = ("proposal", "accepts", "rejects", "withdraws", "undoes_all", "touched_human", "restores")
        events = [e for e in C._read_events(qa.team) if any(k in e for k in keys) or any(
            isinstance(c, dict) and c.get("target") in ("proposal", "freeze", "setting", "checkpoint") for c in e.get("changes") or [])]
        out["_events"] = events
    return out


@contextlib.contextmanager
def mock_limit(name: str, value: int) -> Iterator[None]:
    old = globals()[name]
    globals()[name] = value
    try:
        yield
    finally:
        globals()[name] = old


def _presence_fixture() -> Dict[str, Any]:
    """The presence contract (I-5): the page's POST body and answer, GET and the SSE payload, both record shapes."""
    from herdr_team import canvas_presence as P

    qa_mod = _qa()
    with qa_mod.QaTeam() as qa:
        body = {"page": "0123456789abcdef", "viewport": [0, 0, 1400, 900], "selection": ["E-4", "E-7"], "editing": "E-4", "cursor": [760, 60],
                "away": False}
        human = P.write_human(qa.team, None, body, FIXTURE_NOW)
        member = P.write_member(qa.team, qa.author_for("drawer"), status="drawing", region=[1600, 0, 2400, 400], ids=["E-7", "E-8"],
                                intent="laying out the funnel", ttl_s=P.MEMBER_TTL_S, now=FIXTURE_NOW)
        got = P.read_all(qa.team, FIXTURE_NOW + 1)
    return {"about": "canvas v2 phase 5 presence (9.1, 9.3, I-5): POST /api/teams/<t>/presence takes post_body and answers post_answer; "
                     "GET answers get_answer; the SSE event presence carries sse_data (no id:). A record is fresh while at + ttl_s > now "
                     "and it is not away. The page posts at most 4 a second, a heartbeat every 10 s, away on hide (I-12).",
            "post_body": body, "post_answer": {"ok": True, "ttl_s": P.HUMAN_TTL_S}, "human_record": human, "member_record": member,
            "get_answer": got, "sse_data": dict(got, team=qa_mod.TEAM), "statuses": list(P.STATUSES),
            "limits": {"max_bytes": P.MAX_BYTES, "posts_per_second": 8, "max_ids": P.MAX_IDS, "human_ttl_s": P.HUMAN_TTL_S,
                       "member_ttl_s": P.MEMBER_TTL_S, "look_ttl_s": P.LOOK_TTL_S, "focus_ttl_s": list(P.FOCUS_TTL_RANGE)}}


def _display_fixture() -> Dict[str, Any]:
    """The display-list additions (I-3), taken from the ``collab`` golden: proposal and freeze entries, ``frozen``, ``pending``."""
    path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "display" / "collab.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    entries = [e for e in doc["entries"] if e.get("kind") in ("proposal", "freeze") or e.get("frozen") or e.get("pending")]
    return {"about": "canvas v2 phase 5 display-list entries (4.6, 5.2, I-3), from the collab scene's golden (tests/fixtures/display/collab.json)",
            "generated": True, "scene": "collab", "version": doc["version"], "entries": entries}


def fixture_texts() -> Dict[Path, str]:
    """Every fixture file this module writes, as text."""
    files: Dict[Path, str] = {FIXTURES_DIR / "diff-vectors.json": diff_vectors_text(),
                              FIXTURES_DIR / "presence.json": _json_text(_presence_fixture()),
                              FIXTURES_DIR / "display.json": _json_text(_display_fixture())}
    results = _result_fixtures()
    events = results.pop("_events")
    files[FIXTURES_DIR / "events.json"] = _json_text({
        "about": "canvas v2 phase 5 events the page's store folds (I-6, I-7): the change targets proposal, freeze, setting and "
                 "checkpoint, and the event fields proposal, accepts, rejects, withdraws, undoes_all, touched_human and restores. "
                 "The v1 page skips targets it does not know.", "events": events})
    for name, result in sorted(results.items()):
        files[FIXTURES_DIR / "results" / (name + ".json")] = _json_text(result)
    return files


def main(argv: Sequence[str] = ()) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python3 -m herdr_team.canvas_collab", description="Write or check the collaboration fixtures.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-fixtures", action="store_true", help="rewrite tests/fixtures/collab (diff vectors, results, presence, display)")
    mode.add_argument("--check-fixtures", action="store_true", help="exit 1 when tests/fixtures/collab is stale")
    args = parser.parse_args(list(argv))
    stale = []
    for path, text in sorted(fixture_texts().items()):
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == text:
            continue
        stale.append(os.fspath(path.relative_to(FIXTURES_DIR.parent.parent.parent)))
        if args.write_fixtures:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if stale and args.check_fixtures:
        sys.stderr.write("stale collaboration fixtures (python3 -m herdr_team.canvas_collab --write-fixtures):\n  " + "\n  ".join(stale) + "\n")
        return 1
    for name in stale if args.write_fixtures else ():
        sys.stdout.write("wrote {}\n".format(name))
    return 0


if __name__ == "__main__":
    import sys as _sys

    # The canvas imports this module by its name; run that one, so what the fixtures patch is what the canvas reads.
    from herdr_team import canvas_collab as _module

    raise SystemExit(_module.main(_sys.argv[1:]))
